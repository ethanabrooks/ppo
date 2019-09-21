from collections import namedtuple

from gym.spaces import Box
from torch import nn as nn

from ppo.agent import NNBase
from ppo.distributions import Categorical
from ppo.utils import init, init_normc_

RecurrentState = namedtuple("RecurrentState", "a probs v")
# "planned_probs plan v t state h model_loss"


class Recurrence(NNBase):
    def __init__(
        self, num_inputs, action_space, hidden_size, num_layers, recurrent, activation
    ):
        recurrent_module = nn.GRU if recurrent else None
        super(Recurrence, self).__init__(recurrent_module, num_inputs, hidden_size)

        if recurrent:
            num_inputs = hidden_size

        init_ = lambda m: init(m, init_normc_, lambda x: nn.init.constant_(x, 0))

        layers = []
        in_size = num_inputs
        for _ in range(num_layers):
            layers += [activation, init_(nn.Linear(in_size, hidden_size))]
            in_size = hidden_size
        self.embed1 = nn.Sequential(*layers)

        self.critic = init_(nn.Linear(hidden_size, 1))
        self.actor = Categorical(self.output_size, action_space.n)
        self.continuous = isinstance(action_space, Box)

        self.train()

    @staticmethod
    def sample_new(x, dist):
        new = x < 0
        x[new] = dist.sample()[new].flatten()

    def forward(self, inputs, rnn_hxs, masks, action):
        x = self.embed1(inputs)

        dist = self.actor(x)
        self.sample_new(action, dist)

        return RecurrentState(a=action, probs=dist.probs, v=self.critic(x))
