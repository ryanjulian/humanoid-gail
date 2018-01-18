#!/usr/bin/env python3

import imageio, os, sys
from mpi4py import MPI
import os.path as osp
import gym, logging

from gym.spaces import box
import tensorflow as tf

from baselines import bench, logger
from baselines.common.misc_util import (
    set_global_seeds,
    boolean_flag,
)
from baselines.ppo1 import pposgd_simple, mlp_policy
from baselines.trpo_mpi import trpo_mpi
import baselines.common.tf_util as U
import baselines.ddpg.training as ddpg_training
from baselines.ddpg.models import Actor, Critic
from baselines.ddpg.memory import Memory
from baselines.ddpg.noise import *

from dm_control.rl import environment
from dm_control import suite


class DMSuiteEnv(gym.Env):
    def __init__(self, domain_name="cartpole", task_name="swingup", visualize_reward=True):
        self.dm_env = suite.load(domain_name=domain_name,
                                 task_name=task_name,
                                 visualize_reward=visualize_reward)
        action_spec = self.dm_env.action_spec()
        self.action_space = gym.spaces.Box(low=action_spec.minimum[0],
                                           high=action_spec.maximum[0],
                                           shape=action_spec.shape)
        try:
            ob_spec = self.dm_env.task.observation_spec(self.dm_env.physics)
            self.observation_space = gym.spaces.Box(low=ob_spec.minimum[0],
                                                    high=ob_spec.maximum[0],
                                                    shape=ob_spec.shape)
        except NotImplementedError:
            print("Could not retrieve observation spec, min/max possibly incorrect.", file=sys.stderr)
            # sample observation and set range to [-10, 10]
            ob = self.dm_env.task.get_observation(self.dm_env.physics)
            # ob is an OrderedDict, iterate over all entries to determine overall flattened ob dim
            ob_dimension = 0
            for entry in ob.values():
                ob_dimension += len(entry.flatten())
            self.observation_space = gym.spaces.Box(low=-10,
                                                    high=10,
                                                    shape=(ob_dimension,))
        self.reward_range = (0, 1)
        print('Initialized %s: %s.' % (domain_name, task_name))
        print('\tobservation space: %s (min: %.2f, max: %.2f)' %
              (str(self.observation_space.shape), self.observation_space.low[0], self.observation_space.high[0]))
        print('\taction space: %s (min: %.2f, max: %.2f)' %
              (str(self.action_space.shape), self.action_space.low[0], self.action_space.high[0]))

    def _step(self, action):
        step = self.dm_env.step(action)
        last_step = (step.step_type == environment.StepType.LAST)
        ob = self.observe()
        return ob, step.reward, last_step, {}

    def _reset(self):
        self.dm_env.reset()
        return self.observe()

    def _seed(self, seed=None):
        self.dm_env.random = np.random.RandomState(seed)
        return [seed]

    def observe(self):
        src_ob = self.dm_env.task.get_observation(self.dm_env.physics)
        ob = np.hstack(entry.flatten() for entry in src_ob.values())
        return ob


def train(num_timesteps, num_cpu, method, domain, task, noise_type, layer_norm,
          video_width, video_height, plot_rewards, render_camera, **kwargs):
    rank = MPI.COMM_WORLD.Get_rank()
    if sys.platform == 'darwin':
        num_cpu //= 2
    config = tf.ConfigProto(allow_soft_placement=True,
                            intra_op_parallelism_threads=num_cpu,
                            inter_op_parallelism_threads=num_cpu)
    config.gpu_options.allow_growth = True  # pylint: disable=E1101
    gym.logger.setLevel(logging.WARN)
    tf.Session(config=config).__enter__()
    if rank == 0:
        logger.configure()
    else:
        logger.configure(format_strs=[])

    worker_seed = 1234 + 10000 * MPI.COMM_WORLD.Get_rank()
    set_global_seeds(worker_seed)

    env = DMSuiteEnv(domain, task)
    env.seed(worker_seed)
    logger.info('rank {}: seed={}, logdir={}'.format(rank, worker_seed, logger.get_dir()))

    def policy_fn(name, ob_space, ac_space):  # pylint: disable=W0613
        return mlp_policy.MlpPolicy(name=name, ob_space=ob_space, ac_space=ac_space,
                                    hid_size=64, num_hid_layers=2)

    env = bench.Monitor(env, logger.get_dir() and
                        osp.join(logger.get_dir(), str(rank)),
                        allow_early_resets=True)
    gym.logger.setLevel(logging.WARN)

    def callback(locals, globals):
        if MPI.COMM_WORLD.Get_rank() == 0 and locals['iters_so_far'] % 50 == 0:
            print('Saving video and checkpoint for policy at iteration %i...' % locals['iters_so_far'])
            ob = env.reset()
            images = []
            rewards = []
            lower_part = video_height // 4
            for i in range(1000):
                ac, vpred = locals['pi'].act(False, ob)
                ob, rew, new, _ = env.step(ac)
                if new:
                    break
                img = env.env.dm_env.physics.render(video_width, video_height, camera_id=render_camera)
                if plot_rewards:
                    rewards.append(rew)
                    for j, r in enumerate(rewards):
                        img[-lower_part, :10] = 255
                        img[-lower_part, -10:] = 255
                        rew_x = int(j/1000. * video_width)
                        rew_y = int(r * lower_part)
                        img[-rew_y-1:, rew_x] = 255
                images.append(img)

            imageio.mimsave("videos/%s_%s_%s_iteration_%i.mp4" % (domain, task, method, locals['iters_so_far']),
                            images, fps=60)
            env.reset()
            U.save_state(os.path.join("checkpoints", "%s_%s_%i" % (domain, task, locals['iters_so_far'])))

    if method == "ppo":
        pposgd_simple.learn(env, policy_fn,
                            max_timesteps=int(num_timesteps),
                            timesteps_per_actorbatch=1024,  # 256
                            clip_param=0.2,
                            entcoeff=0.01,
                            optim_epochs=4,
                            optim_stepsize=1e-3,  # 1e-3
                            optim_batchsize=64,
                            gamma=0.99,
                            lam=0.95,
                            schedule='constant',  # 'linear'
                            callback=callback
                            )
    elif method == "trpo":
        trpo_mpi.learn(env, policy_fn,
                       max_timesteps=int(num_timesteps),
                       timesteps_per_batch=1024, max_kl=0.01, cg_iters=10, cg_damping=0.1,
                       gamma=0.99, lam=0.98, vf_iters=5, vf_stepsize=1e-3,
                       callback=callback
                       )
    elif method == "ddpg":
        # Parse noise_type
        action_noise = None
        param_noise = None
        nb_actions = env.action_space.shape[-1]
        for current_noise_type in noise_type.split(','):
            current_noise_type = current_noise_type.strip()
            if current_noise_type == 'none':
                pass
            elif 'adaptive-param' in current_noise_type:
                _, stddev = current_noise_type.split('_')
                param_noise = AdaptiveParamNoiseSpec(initial_stddev=float(stddev), desired_action_stddev=float(stddev))
            elif 'normal' in current_noise_type:
                _, stddev = current_noise_type.split('_')
                action_noise = NormalActionNoise(mu=np.zeros(nb_actions), sigma=float(stddev) * np.ones(nb_actions))
            elif 'ou' in current_noise_type:
                _, stddev = current_noise_type.split('_')
                action_noise = OrnsteinUhlenbeckActionNoise(mu=np.zeros(nb_actions),
                                                            sigma=float(stddev) * np.ones(nb_actions))
            else:
                raise RuntimeError('unknown noise type "{}"'.format(current_noise_type))

        # Configure components.
        memory = Memory(limit=int(1e6), action_shape=env.action_space.shape,
                        observation_shape=env.observation_space.shape)
        critic = Critic(layer_norm=layer_norm)
        actor = Actor(nb_actions, layer_norm=layer_norm)

        ddpg_training.train(env=env, eval_env=None, param_noise=param_noise, render=False, render_eval=False,
                            action_noise=action_noise, actor=actor, critic=critic, memory=memory, **kwargs)
    else:
        print('ERROR: Invalid "method" argument provided.', file=sys.stderr)
    env.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--num-timesteps', type=int, default=int(10e6))
    parser.add_argument('--num-cpu', help='number of cpu to used', type=int, default=4)
    parser.add_argument('--method', help='reinforcement learning algorithm to use (ppo/trpo/ddpg)',
                        type=str, default='ppo')
    parser.add_argument('--domain', help='domain to use for the RL environment from DM Control Suite',
                        type=str, default='cartpole')
    parser.add_argument('--task', help='task to use for the RL environment from DM Control Suite',
                        type=str, default='swingup')

    # DDPG settings
    boolean_flag(parser, 'normalize-returns', default=False)
    boolean_flag(parser, 'normalize-observations', default=True)
    boolean_flag(parser, 'layer-norm', default=True)
    parser.add_argument('--critic-l2-reg', type=float, default=1e-2)
    parser.add_argument('--batch-size', type=int, default=64)  # per MPI worker
    parser.add_argument('--actor-lr', type=float, default=1e-4)
    parser.add_argument('--critic-lr', type=float, default=1e-3)
    boolean_flag(parser, 'popart', default=False)
    parser.add_argument('--gamma', type=float, default=0.99)
    parser.add_argument('--reward-scale', type=float, default=1.)
    parser.add_argument('--clip-norm', type=float, default=None)
    parser.add_argument('--nb-epochs', type=int, default=500)  # with default settings, perform 1M steps total
    parser.add_argument('--nb-epoch-cycles', type=int, default=20)
    parser.add_argument('--nb-train-steps', type=int, default=50)  # per epoch cycle and MPI worker
    parser.add_argument('--nb-eval-steps', type=int, default=100)  # per epoch cycle and MPI worker
    parser.add_argument('--nb-rollout-steps', type=int, default=100)  # per epoch cycle and MPI worker
    parser.add_argument('--noise-type', type=str,
                        default='adaptive-param_0.2')  # choices are adaptive-param_xx, ou_xx, normal_xx, none

    # resolution of rendered videos
    parser.add_argument('--video-width', type=int, default=400)
    parser.add_argument('--video-height', type=int, default=400)
    boolean_flag(parser, 'plot-rewards', default=True)
    parser.add_argument('--render-camera', type=int, default=1)

    args = parser.parse_args()
    train(**vars(args))


if __name__ == '__main__':
    main()
