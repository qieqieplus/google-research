# coding=utf-8
# Copyright 2022 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

r"""Implicit aux tasks training.

Example command:

python -m aux_tasks.synthetic.run_synthetic

"""
# pylint: disable=invalid-name
import functools
import pickle

from absl import app
from absl import flags
from absl import logging
from clu import checkpoint
from clu import metric_writers
from clu import periodic_actions
from etils import epath
from etils import etqdm
import jax
import jax.numpy as jnp
from ml_collections import config_dict
from ml_collections import config_flags
import numpy as np

from aux_tasks.synthetic import estimates
from aux_tasks.synthetic import utils

_config = config_dict.ConfigDict()

_config.method: str = 'explicit'
_config.optimizer: str = 'sgd'
_config.num_epochs: int = 200_000
_config.rescale_psi = ''

_config.S: int = 10  # Number of states
_config.T: int = 10  # Number of aux. tasks
_config.d: int = 1  # feature dimension

_config.estimate_feature_norm: bool = True

_config.kappa: float = 0.9  # Lissa kappa

_config.covariance_batch_size: int = 32
_config.main_batch_size: int = 32
_config.weight_batch_size: int = 32

_config.seed: int = 4753849
_config.lr: float = 0.01

_WORKDIR = flags.DEFINE_string(
    'workdir', None, 'Base directory to store stats.', required=True)
_CONFIG = config_flags.DEFINE_config_dict('config', _config, lock_config=True)


def compute_optimal_subspace(Psi, d):
  left_svd, _, _ = jnp.linalg.svd(Psi)
  return left_svd[:, :d]


def compute_grassman_distance(Y1, Y2):
  """Grassman distance between subspaces spanned by Y1 and Y2."""
  Q1, _ = jnp.linalg.qr(Y1)
  Q2, _ = jnp.linalg.qr(Y2)

  _, sigma, _ = jnp.linalg.svd(Q1.T @ Q2)
  sigma = jnp.round(sigma, decimals=6)
  return jnp.linalg.norm(jnp.arccos(sigma))


def compute_cosine_similarity(Y1, Y2):
  try:
    projection_weights = jnp.linalg.solve(Y1.T @ Y1, Y1.T @ Y2)
    projection = Y1 @ projection_weights

    return jnp.linalg.norm(projection)
  except np.linalg.LinAlgError:
    pass
  return jnp.nan


def compute_normalized_dot_product(
    Y1, Y2):
  return jnp.abs(
      jnp.squeeze(Y1.T @ Y2 / (jnp.linalg.norm(Y1) * jnp.linalg.norm(Y2))))


def eigengame_subspace_distance(
    Phi, optimal_subspace):
  """Compute subspace distance as per the eigengame paper."""
  try:
    d = Phi.shape[1]
    U_star = optimal_subspace @ optimal_subspace.T

    U_phi, _, _ = jnp.linalg.svd(Phi)
    U_phi = U_phi[:, :d]
    P_star = U_phi @ U_phi.T

    return 1 - 1 / d * jnp.trace(U_star @ P_star)
  except np.linalg.LinAlgError:
    return jnp.nan


def compute_metrics(
    Phi, optimal_subspace):
  """Computes a variety of learning curve-type metrics for the given run.

  Args:
    Phi: Feature matrix.
    optimal_subspace: The optimal subspace.

  Returns:
    dict with keys:
      cosine_similarity: a jnp.array of size num_update_steps with cosine
        similarity between Phi and the d-principal subspace of Psi.
      feature_norm: the mean norm of the state feature vectors
        (averaged across states) over time.
  """
  feature_norm = jnp.linalg.norm(Phi) / Phi.shape[0]
  cosine_similarity = compute_cosine_similarity(Phi, optimal_subspace)

  metrics = {
      'cosine_similarity':
          cosine_similarity,
      'feature_norm':
          feature_norm,
      'eigengame_subspace_distance':
          eigengame_subspace_distance(Phi, optimal_subspace)
  }

  _, d = Phi.shape
  if d > 1:
    grassman_distance = compute_grassman_distance(Phi, optimal_subspace)
    metrics |= {'grassman_distance': grassman_distance}
  elif d == 1:
    dot_product = compute_normalized_dot_product(Phi, optimal_subspace)
    metrics |= {'dot_product': dot_product}

  return metrics


@functools.partial(jax.jit, static_argnames=(
    'method',
    'covariance_batch_size',
    'main_batch_size',
    'weight_batch_size',
    'estimate_feature_norm',))
def _train_step(
    *,
    Phi,
    Psi,
    explicit_weight_matrix,
    estimated_feature_norm,
    learning_rate,
    key,
    method,
    lissa_kappa,
    covariance_batch_size,
    main_batch_size,
    weight_batch_size,
    estimate_feature_norm = True):
  """Computes one training step.

  Args:
    Phi: The current feature matrix.
    Psi: The target matrix whose PCA is to be determined.
    explicit_weight_matrix: A weight matrix to use for the explicit method.
    estimated_feature_norm: The current estimated feature norm.
    learning_rate: The step size parameter for sgd.
    key: The jax prng key.
    method: 'naive', 'lissa', or 'oracle'.
    lissa_kappa: The parameter of the lissa method, if used.
    covariance_batch_size: the 'J' parameter. For the naive method, this is how
      many states we sample to construct the inverse. For the lissa method,
      ditto -- these are also "iterations".
    main_batch_size: How many states to update at once.
    weight_batch_size: How many states to construct the weight vector.
    estimate_feature_norm: Whether to use a running average of the max feature
      norm rather than the real maximum.

  Returns:
    A dict containing updated values for Phi, estimated_feature_norm, and key,
      as well as the the computed gradient.
  """
  num_states, d = Phi.shape
  _, num_tasks = Psi.shape

  # Draw one or many source states to update, and its task.
  source_states, key = utils.draw_states(num_states, main_batch_size, key)
  task, key = utils.draw_states(num_tasks, 1, key)  # bad Marc!

  # Use the source states to update our estimate of the feature norm.
  # Do this pre-LISSA, avoid a bad first gradient.
  if method == 'lissa' and estimate_feature_norm:
    features = Phi[source_states, :]
    max_norm = utils.compute_max_feature_norm(features)
    estimated_feature_norm = (
        estimated_feature_norm + 0.01 * (max_norm - estimated_feature_norm))

  ### This determines the weight vectors to be used to perform the gradient
  ### step.
  if method == 'explicit':
    # With the explicit method we maintain a running weight vector.
    # TODO(bellemare): This assumes we are sampling exactly one task. But
    # other parts of the code are actually also dependent on this point...
    weight_1 = jnp.squeeze(explicit_weight_matrix[:, task], axis=1)
    weight_2 = jnp.squeeze(explicit_weight_matrix[:, task], axis=1)
  else:  # Implicit methods.
    # Please resist the urge to refactor this code for now.
    if method == 'oracle':
      # This exactly determines the covariance.
      covariance_1 = jnp.linalg.pinv(Phi.T @ Phi) * num_states
      covariance_2 = covariance_1

      # Use all states for weight vector.
      weight_states_1 = jnp.arange(0, num_states)
      weight_states_2 = weight_states_1
    if method == 'naive':
      # The naive method uses one covariance matrix for both weight vectors.
      covariance_1, key = estimates.naive_inverse_covariance_matrix(
          Phi, key, covariance_batch_size)
      covariance_2 = covariance_1

      weight_states_1, key = utils.draw_states(
          num_states, weight_batch_size, key)
      weight_states_2 = weight_states_1
    elif method == 'naive++':
      # The naive method uses one covariance matrix for both weight vectors.
      covariance_1, key = estimates.naive_inverse_covariance_matrix(
          Phi, key, covariance_batch_size)
      covariance_2, key = estimates.naive_inverse_covariance_matrix(
          Phi, key, covariance_batch_size)

      weight_states_1, key = utils.draw_states(
          num_states, weight_batch_size, key)
      weight_states_2, key = utils.draw_states(
          num_states, weight_batch_size, key)
    elif method == 'lissa':
      # Compute two independent estimates of the inverse covariance matrix.
      covariance_1, key = estimates.lissa_inverse_covariance_matrix(
          Phi, key, covariance_batch_size, lissa_kappa, None)
      covariance_2, key = estimates.lissa_inverse_covariance_matrix(
          Phi, key, covariance_batch_size, lissa_kappa, None)

      # Draw two separate sets of states for the weight vectors (important!)
      weight_states_1, key = utils.draw_states(
          num_states, weight_batch_size, key)
      weight_states_2, key = utils.draw_states(
          num_states, weight_batch_size, key)

    # Compute the weight estimates by combining the inverse covariance
    # estimate and the sampled Phi & Psi's.
    weight_1 = (covariance_1 @ Phi[weight_states_1, :].T
                @ Psi[weight_states_1, task]) / len(weight_states_1)
    weight_2 = (covariance_2 @ Phi[weight_states_2, :].T
                @ Psi[weight_states_2, task]) / len(weight_states_2)

  # Compute the gradient at that source state.
  estimated_error = (
      jnp.dot(Phi[source_states, :], weight_1) - Psi[source_states, task])

  # We use the same weight vector to move all elements of our batch, but
  # they have different errors.
  gradient = jnp.reshape(
      jnp.tile(weight_2, main_batch_size), (main_batch_size, d))

  # Line up the shapes of error and weight vectors so we can construct the
  # gradient.
  expanded_estimated_error = jnp.expand_dims(estimated_error, axis=1)
  gradient = gradient * expanded_estimated_error

  # Apply the gradient update (sgd).
  # This will only work with numpy, but so much cleaner than the jax version
  # Phi[source_states, :] -= learning_rate * gradient
  # Jax version (untested):
  Phi = Phi.at[source_states, :].set(
      Phi[source_states, :] - learning_rate * gradient)

  if method == 'explicit':
    # Also update the weight vector for this task.
    weight_gradient = Phi[source_states, :].T @ estimated_error
    expanded_gradient = jnp.expand_dims(weight_gradient, axis=1)
    explicit_weight_matrix[:, task] -= learning_rate * expanded_gradient

  return {
      'Phi': Phi,
      'estimated_feature_norm': estimated_feature_norm,
      'key': key,
      'gradient': gradient,
      }


def train(*,
          workdir,
          initial_step,
          chkpt_manager,
          Phi,
          Psi,
          optimal_subspace,
          num_epochs,
          learning_rate,
          key,
          method,
          lissa_kappa,
          optimizer,
          covariance_batch_size,
          main_batch_size,
          weight_batch_size,
          estimate_feature_norm = True):
  """Training function.

  For lissa, the total number of samples is
  2 x covariance_batch_size + main_batch_size + 2 x weight_batch_size.

  Args:
    workdir: Work directory, where we'll save logs.
    initial_step: Initial step
    chkpt_manager: Checkpoint manager.
    Phi: The initial feature matrix.
    Psi: The target matrix whose PCA is to be determined.
    optimal_subspace: Top-d left singular vectors of Psi.
    num_epochs: How many gradient steps to perform. (Not really epochs)
    learning_rate: The step size parameter for sgd.
    key: The jax prng key.
    method: 'naive', 'lissa', or 'oracle'.
    lissa_kappa: The parameter of the lissa method, if used.
    optimizer: Which optimizer to use. Only 'sgd' is supported.
    covariance_batch_size: the 'J' parameter. For the naive method, this is how
      many states we sample to construct the inverse. For the lissa method,
      ditto -- these are also "iterations".
    main_batch_size: How many states to update at once.
    weight_batch_size: How many states to construct the weight vector.
    estimate_feature_norm: Whether to use a running average of the max feature
      norm rather than the real maximum.

  Returns:
    A matrix of all Phis computed throughout training. This will be of shape
        (num_epochs, d, d).
  """
  # Don't overwrite Phi.
  Phi = jnp.copy(Phi)
  Phis = [jnp.copy(Phi)]

  _, d = Phi.shape
  _, num_tasks = Psi.shape

  # Keep a running average of the max norm of a feature vector. None means:
  # don't do it.
  if estimate_feature_norm:
    estimated_feature_norm = utils.compute_max_feature_norm(Phi)
  else:
    estimated_feature_norm = None

  # Create an explicit weight vector (needed for explicit method).
  key, weight_key = jax.random.split(key)
  explicit_weight_matrix = jax.random.normal(
      weight_key, (d, num_tasks), dtype=jnp.float64)

  assert optimizer == 'sgd', 'Non-sgd not yet supported.'

  writer = metric_writers.create_default_writer(
      logdir=str(workdir),
  )

  hooks = [
      periodic_actions.PeriodicCallback(
          every_steps=5_000,
          callback_fn=lambda step, t: chkpt_manager.save((step, Phi)))
  ]

  fixed_train_kwargs = {
      'Psi': Psi,
      'explicit_weight_matrix': explicit_weight_matrix,
      'learning_rate': learning_rate,
      'method': method,
      'lissa_kappa': lissa_kappa,
      'covariance_batch_size': covariance_batch_size,
      'main_batch_size': main_batch_size,
      'weight_batch_size': weight_batch_size,
      'estimate_feature_norm': estimate_feature_norm,
  }
  variable_kwargs = {
      'Phi': Phi,
      'estimated_feature_norm': estimated_feature_norm,
      'key': key,
  }

  # Perform num_epochs gradient steps.
  with metric_writers.ensure_flushes(writer):
    for step in etqdm.tqdm(
        range(initial_step + 1, num_epochs + 1),
        initial=initial_step,
        total=num_epochs):

      variable_kwargs = _train_step(**fixed_train_kwargs, **variable_kwargs)
      gradient = variable_kwargs.pop('gradient')

      Phi = variable_kwargs['Phi']
      metrics = compute_metrics(Phi, optimal_subspace)
      metrics |= {'grad_norm': jnp.linalg.norm(gradient)}
      metrics |= {'frob_norm': utils.outer_objective_mc(Phi, Psi)}
      writer.write_scalars(step, metrics)

      Phis.append(jnp.copy(Phi))

      for hook in hooks:
        hook(step)

  return jnp.stack(Phis)


def main(_):
  jax.config.update('jax_enable_x64', True)

  config: config_dict.ConfigDict = _CONFIG.value
  logging.info(config)

  key = jax.random.PRNGKey(config.seed)
  key, psi_key, phi_key = jax.random.split(key, 3)
  Psi = jax.random.normal(psi_key, (config.S, config.T), dtype=jnp.float64)
  if config.rescale_psi == 'linear':
    Psi = utils.generate_psi_linear(Psi)
  elif config.rescale_psi == 'exp':
    Psi = utils.generate_psi_exp(Psi)

  Phi = jax.random.normal(phi_key, (config.S, config.d), dtype=jnp.float64)

  chkpt_manager = checkpoint.Checkpoint(base_directory=_WORKDIR.value)

  initial_step = 0
  initial_step, Phi = chkpt_manager.restore_or_initialize((initial_step, Phi))

  optimal_subspace = compute_optimal_subspace(Psi, config.d)

  workdir = epath.Path(_WORKDIR.value)
  workdir.mkdir(exist_ok=True)

  Phis = train(
      workdir=workdir,
      initial_step=initial_step,
      chkpt_manager=chkpt_manager,
      Phi=Phi,
      Psi=Psi,
      optimal_subspace=optimal_subspace,
      num_epochs=config.num_epochs,
      learning_rate=config.lr,
      key=key,
      method=config.method,
      lissa_kappa=config.kappa,
      optimizer=config.optimizer,
      covariance_batch_size=config.covariance_batch_size,
      main_batch_size=config.main_batch_size,
      weight_batch_size=config.weight_batch_size,
      estimate_feature_norm=config.estimate_feature_norm)

  with (workdir / 'phis.pkl').open('wb') as fout:
    pickle.dump(Phis, fout, protocol=4)


if __name__ == '__main__':
  app.run(main)
