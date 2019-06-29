from collections import namedtuple

import gym
from gym import spaces
from gym.spaces import Discrete
import numpy as np

Actions = namedtuple('Actions', 'a cr cg g')


class DebugWrapper(gym.Wrapper):
    def __init__(self, env):
        super().__init__(env)
        self.last_guess = None
        self.last_reward = None
        action_spaces = Actions(*env.action_space.spaces)
        for x in action_spaces:
            assert isinstance(x, Discrete)
        self.action_sections = len(action_spaces)
        self.truth = None

    def step(self, action):
        actions = Actions(*[x.item() for x in np.split(action, self.action_sections)])
        s, _, t, i = super().step(action)
        guess = int(actions.g)
        env = self.env.unwrapped
        truth = int(env.subtask_idx)
        if truth > env.n_subtasks:  # truth is out of bounds
            truth = self.truth  # keep truth at old value

        r = float(np.all(guess == truth)) - 1
        # if r < 0:
        #     import ipdb
        #     ipdb.set_trace()

        self.truth = truth
        self.last_guess = guess
        self.last_reward = r
        return s, r, t, i

    def render(self, mode='human'):
        print('########################################')
        super().render(sleep_time=0)
        print('guess', self.last_guess)
        print('truth', self.env.unwrapped.subtask_idx)
        print('$$$$$$$$$$$$$$')
        print('$ reward', self.last_reward, '$')
        print('$$$$$$$$$$$$$$')
        # input('pause')


class Wrapper(gym.Wrapper):
    def __init__(self, env):
        super().__init__(env)
        self.action_space = spaces.Dict(
            Actions(
                a=env.action_space,
                g=spaces.Discrete(env.n_subtasks),
                cg=spaces.Discrete(2),
                cr=spaces.Discrete(2),
            )._asdict())
        self.last_g = None

    def step(self, action):
        actions = Actions(*np.split(action, len(self.action_space.spaces)))
        action = int(actions.a)
        self.last_g = int(actions.g)
        return super().step(action)

    def render(self, mode='human', **kwargs):
        super().render(mode=mode)
        if self.last_g is not None:
            env = self.env.unwrapped
            subtask = env.subtasks[self.last_g]
            print('Assigned subtask:', f'{self.last_g}:{subtask}')
        input('paused')
