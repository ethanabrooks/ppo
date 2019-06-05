from collections import namedtuple

import torch
import torch.nn as nn
from gym.spaces import Box, Discrete

from ppo.distributions import Categorical, DiagGaussian
from ppo.utils import init, init_normc_

AgentValues = namedtuple('AgentValues',
                         'value action action_log_probs aux_loss rnn_hxs log')


class Flatten(nn.Module):
    def forward(self, x):
        return x.view(x.size(0), -1)


class Agent(nn.Module):
    def __init__(self,
                 obs_shape,
                 action_space,
                 recurrent,
                 hidden_size,
                 entropy_coef,
                 logic=False,
                 **network_args):
        super(Agent, self).__init__()
        self.entropy_coef = entropy_coef
        if network_args is None:
            network_args = {}
        if logic:
            self.base = LogicBase(
                *obs_shape,
                hidden_size=hidden_size)
        elif len(obs_shape) == 3:
            self.base = CNNBase(
                *obs_shape, recurrent=recurrent, hidden_size=hidden_size)
        elif len(obs_shape) == 1:
            self.base = MLPBase(
                obs_shape[0],
                recurrent=recurrent,
                hidden_size=hidden_size,
                **network_args)
        else:
            raise NotImplementedError

        if isinstance(action_space, Discrete):
            num_outputs = action_space.n
            self.dist = Categorical(self.base.output_size, num_outputs)
        elif isinstance(action_space, Box):
            num_outputs = action_space.shape[0]
            self.dist = DiagGaussian(self.base.output_size, num_outputs)
        else:
            raise NotImplementedError
        self.continuous = isinstance(action_space, Box)

    @property
    def is_recurrent(self):
        return self.base.is_recurrent

    @property
    def recurrent_hidden_state_size(self):
        """Size of rnn_hx."""
        return self.base.recurrent_hidden_state_size

    def forward(self, inputs, rnn_hxs, masks, deterministic=False,
                action=None):
        value, actor_features, rnn_hxs = self.base(inputs, rnn_hxs, masks)

        dist = self.dist(actor_features)

        if action is None:
            if deterministic:
                action = dist.mode()
            else:
                action = dist.sample()

        action_log_probs = dist.log_probs(action)
        entropy = dist.entropy().mean()
        return AgentValues(
            value=value,
            action=action,
            action_log_probs=action_log_probs,
            aux_loss=-self.entropy_coef * entropy,
            rnn_hxs=rnn_hxs,
            log=dict(entropy=entropy))

    def get_value(self, inputs, rnn_hxs, masks):
        value, _, _ = self.base(inputs, rnn_hxs, masks)
        return value


class NNBase(nn.Module):
    def __init__(self, recurrent: bool, recurrent_input_size, hidden_size):
        super(NNBase, self).__init__()

        self._hidden_size = hidden_size
        self._recurrent = recurrent

        if self._recurrent:
            self.recurrent_module = self.build_recurrent_network(
                recurrent_input_size, hidden_size)
            for name, param in self.recurrent_module.named_parameters():
                print('zeroed out', name)
                if 'bias' in name:
                    nn.init.constant_(param, 0)
                elif 'weight' in name:
                    nn.init.orthogonal_(param)

    def build_recurrent_network(self, input_size, hidden_size):
        return nn.GRU(input_size, hidden_size)

    @property
    def is_recurrent(self):
        return self._recurrent

    @property
    def recurrent_hidden_state_size(self):
        if self._recurrent:
            return self._hidden_size
        return 1

    @property
    def output_size(self):
        return self._hidden_size

    def _forward_gru(self, x, hxs, masks):
        if x.size(0) == hxs.size(0):
            x, hxs = self.recurrent_module(
                x.unsqueeze(0), (hxs * masks).unsqueeze(0))
            x = x.squeeze(0)
            hxs = hxs.squeeze(0)
        else:
            # x is a (T, N, -1) tensor that has been flatten to (T * N, -1)
            N = hxs.size(0)
            T = int(x.size(0) / N)

            # unflatten
            x = x.view(T, N, *x.shape[1:])

            # Same deal with masks
            masks = masks.view(T, N)

            # Let's figure out which steps in the sequence have a zero for any agent
            # We will always assume t=0 has a zero in it as that makes the logic cleaner
            has_zeros = ((masks[1:] == 0.0).any(
                dim=-1).nonzero().squeeze().cpu())

            # +1 to correct the masks[1:]
            if has_zeros.dim() == 0:
                # Deal with scalar
                has_zeros = [has_zeros.item() + 1]
            else:
                has_zeros = (has_zeros + 1).numpy().tolist()

            # add t=0 and t=T to the list
            has_zeros = [0] + has_zeros + [T]

            hxs = hxs.unsqueeze(0)
            outputs = []
            for i in range(len(has_zeros) - 1):
                # We can now process steps that don't have any zeros in masks together!
                # This is much faster
                start_idx = has_zeros[i]
                end_idx = has_zeros[i + 1]

                rnn_scores, hxs = self.recurrent_module(
                    x[start_idx:end_idx],
                    hxs * masks[start_idx].view(1, -1, 1))

                outputs.append(rnn_scores)

            # assert len(outputs) == T
            # x is a (T, N, -1) tensor
            x = torch.cat(outputs, dim=0)
            # flatten
            x = x.view(T * N, -1)
            hxs = hxs.squeeze(0)

        return x, hxs


class LogicModule(nn.Module):
    def __init__(self, h, w, d, similarity_measure, hidden_size):
        super().__init__()
        self._hidden_size = hidden_size
        self.similarity_measure = similarity_measure
        init_ = lambda m: init(m, nn.init.orthogonal_, lambda x: nn.init.
                               constant_(x, 0), nn.init.calculate_gain('relu'))
        self.conv = init_(
            nn.Conv2d(d, hidden_size, kernel_size=3, stride=1, padding=1))

        self.mlp = nn.Sequential(
            nn.ReLU(), Flatten(),
            init_(nn.Linear(hidden_size * h * w, hidden_size * 2)), nn.ReLU())
        self.main = nn.Sequential(self.conv, self.mlp)

        init_ = lambda m: init(m, nn.init.orthogonal_, lambda x: nn.init.
                               constant_(x, 0))

        self.critic_linear = init_(nn.Linear(hidden_size, 1))

    def forward_one_step(self, x, hx):
        initial_shape = hx.shape
        if torch.all(hx == 0):  # first step of episode
            hx = x[:, -1]  # to do objects
        iterate = x[:, -2]  # whether to shift attention
        hx = hx.view(*x[:, -1].shape)
        conv_inputs = torch.cat([x[:, :-1], hx.unsqueeze(1)], dim=1)
        conv_out = self.conv(conv_inputs)
        sections = [self._hidden_size] * 2
        key, x = torch.split(self.mlp(conv_out), sections, dim=-1)
        key = key.view(*key.shape, 1, 1)

        if self.similarity_measure == 'dot-product':
            similarity = torch.sum(key * conv_out, dim=1)
        elif self.similarity_measure == 'euclidean-distance':
            similarity = torch.norm(key - conv_out, dim=1)
        elif self.similarity_measure == 'cosine-similarity':
            similarity = torch.nn.functional.cosine_similarity(
                key, conv_out, dim=1)

        hx = hx.squeeze(1) - iterate * similarity
        return x, hx.view(initial_shape)

    def forward(self, input, hx=None):
        assert hx is not None
        hx = hx[0]  # remaining hxs are generated dynamically
        outputs = []
        for x in input:
            x, hx = self.forward_one_step(x, hx)
            outputs.append((x, hx))

        xs, hxs = zip(*outputs)
        return torch.stack(xs), torch.stack(hxs)


class LogicBase(NNBase):
    def __init__(self, d, h, w, similarity_measure, hidden_size=512):
        self.input_shape = h, w, d
        self.similarity_measure = similarity_measure

        super(LogicBase, self).__init__(
            recurrent=True,
            recurrent_input_size=h * w,
            hidden_size=hidden_size)
        self._recurrent_hidden_state_size = h * w

        init_ = lambda m: init(m, nn.init.orthogonal_, lambda x: nn.init.
                               constant_(x, 0), nn.init.calculate_gain('relu'))
        self.critic_linear = init_(nn.Linear(hidden_size, 1))
        self.train()

    def build_recurrent_network(self, input_size, hidden_size):
        return LogicModule(
            *self.input_shape,
            hidden_size=hidden_size,
            similarity_measure=self.similarity_measure)

    @property
    def recurrent_hidden_state_size(self):
        return self._recurrent_hidden_state_size

    def forward(self, inputs, rnn_hxs, masks):
        x, rnn_hxs = self._forward_gru(inputs, rnn_hxs, masks)
        return self.critic_linear(x), x, rnn_hxs


class CNNBase(NNBase):
    def __init__(self, d, h, w, hidden_size, recurrent=False):
        super(CNNBase, self).__init__(recurrent, hidden_size, hidden_size)

        init_ = lambda m: init(m, nn.init.orthogonal_, lambda x: nn.init.
                               constant_(x, 0), nn.init.calculate_gain('relu'))

        self.main = nn.Sequential(
            init_(
                nn.Conv2d(d, hidden_size, kernel_size=3, stride=1, padding=1)),
            # init_(nn.Conv2d(d, 32, 8, stride=4)), nn.ReLU(),
            # init_(nn.Conv2d(32, 64, kernel_size=4, stride=2)), nn.ReLU(),
            # init_(nn.Conv2d(32, 64, kernel_size=4, stride=2)), nn.ReLU(),
            # init_(nn.Conv2d(64, 32, kernel_size=3, stride=1)),
            nn.ReLU(),
            Flatten(),
            # init_(nn.Linear(32 * 7 * 7, hidden_size)), nn.ReLU())
            init_(nn.Linear(hidden_size * h * w, hidden_size)),
            nn.ReLU())

        init_ = lambda m: init(m, nn.init.orthogonal_, lambda x: nn.init.
                               constant_(x, 0))

        self.critic_linear = init_(nn.Linear(hidden_size, 1))

        self.train()

    def forward(self, inputs, rnn_hxs, masks):
        x = self.main(inputs)

        if self.is_recurrent:
            x, rnn_hxs = self._forward_gru(x, rnn_hxs, masks)

        return self.critic_linear(x), x, rnn_hxs


class MLPBase(NNBase):
    def __init__(self, num_inputs, hidden_size, num_layers, recurrent,
                 activation):
        recurrent_module = nn.GRU if recurrent else None
        super(MLPBase, self).__init__(recurrent_module, num_inputs,
                                      hidden_size)

        if recurrent:
            num_inputs = hidden_size

        init_ = lambda m: init(m, init_normc_, lambda x: nn.init.constant_(
            x, 0))

        self.actor = nn.Sequential()
        self.critic = nn.Sequential()
        for i in range(num_layers):
            in_features = num_inputs if i == 0 else hidden_size
            self.actor.add_module(
                name=f'fc{i}',
                module=nn.Sequential(
                    init_(nn.Linear(in_features, hidden_size)),
                    activation,
                ))
            self.critic.add_module(
                name=f'fc{i}',
                module=nn.Sequential(
                    init_(nn.Linear(in_features, hidden_size)),
                    activation,
                ))

        self.critic_linear = init_(nn.Linear(hidden_size, 1))

        self.train()

    def forward(self, inputs, rnn_hxs, masks):
        x = inputs

        if self.is_recurrent:
            x, rnn_hxs = self._forward_gru(x, rnn_hxs, masks)

        hidden_critic = self.critic(x)
        hidden_actor = self.actor(x)

        return self.critic_linear(hidden_critic), hidden_actor, rnn_hxs
