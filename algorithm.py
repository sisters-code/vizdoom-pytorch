# -*- coding: utf-8 -*-

import vizdoom as vzd
import torch
import torch.optim as optim
import numpy as np
import random
import itertools as it
import skimage.transform
from vizdoom import Mode
from time import sleep, time
from collections import deque
from tqdm import trange
from tensorboardX import SummaryWriter
from network import Network

# Q-learning settings
learning_rate = 0.00025
discount_factor = 0.99
train_epochs = 10000
learning_steps_per_epoch = 1000
replay_memory_size = 2000 # num of replays saved

# NN learning settings
batch_size = 12

# Training regime
test_episodes_per_epoch = 1000

# Other parameters
frame_repeat = 12
resolution = (160, 120)
episodes_to_watch = 1000

model_savefile = "./model-doom-variable.pth"
save_model = True
load_model = False
skip_learning = False

# Configuration file path
# config_file_path = "../../../scenarios/simpler_basic.cfg"
# config_file_path = "../../../scenarios/rocket_basic.cfg"
config_file_path = "../../../scenarios/basic_2.cfg"

# Uses GPU if available
if torch.cuda.is_available():
    DEVICE = torch.device('cuda')
    torch.backends.cudnn.benchmark = True
else:
    DEVICE = torch.device('cpu')

# def get_reward(game_state):
#     misc = game_state.game_variables
#     #  reward setting
#     health_reward = 10 * (misc[1] - prev_misc[1])
#     kill_reward = 1000 * (misc[2] - prev_misc[2])
#     return health_reward + kill_reward
def preprocess(img):
    """Down samples image to resolution"""
    img = skimage.transform.resize(img, resolution)
    img = img.astype(np.float32)
    img = np.expand_dims(img, axis=0)
    # img = img.transpose(2, 0, 1)
    return img


def create_simple_game():
    print("Initializing doom...")
    game = vzd.DoomGame()
    game.load_config(config_file_path)
    game.set_window_visible(False)
    game.set_mode(Mode.PLAYER)
    # game.set_screen_format(vzd.ScreenFormat.RGB24)
    game.set_screen_format(vzd.ScreenFormat.GRAY8)
    game.set_screen_resolution(vzd.ScreenResolution.RES_640X480)
    game.init()
    print("Doom initialized.")

    return game


def test(game, agent):
    """Runs a test_episodes_per_epoch episodes and prints the result"""
    print("\nTesting...")
    test_scores = []

    for test_episode in trange(test_episodes_per_epoch, leave=False):
        game.new_episode()
        episode_rewards = 0
        misc = [50, 100, 0]
        while not game.is_episode_finished():
            game_state = game.get_state()
            prev_misc = game_state.game_variables
            state = preprocess(game_state.screen_buffer)

            #  reward setting
            health_reward = 10 * (prev_misc[1] - misc[1])
            kill_reward = 1000 * (prev_misc[2] - misc[2])
            reward = health_reward + kill_reward

            best_action_index = agent.get_action(state)
            game.set_action(actions[best_action_index])
            misc = game_state.game_variables
            # game.make_action(actions[best_action_index], frame_repeat)
            episode_rewards += reward
            for _ in range(frame_repeat):
                game.advance_action()

        r = game.get_total_reward() + episode_rewards
        print("Total score: ", r)
        test_scores.append(r)

    test_scores = np.array(test_scores)
    print("Results: mean: %.1f +/- %.1f," % (
        test_scores.mean(), test_scores.std()), "min: %.1f" % test_scores.min(),
          "max: %.1f" % test_scores.max())


def run(game, agent, actions, num_epochs, frame_repeat, steps_per_epoch=2000):
    """
    Run num epochs of training episodes.
    Skip frame_repeat number of frames after each action.
    """

    start_time = time()
    prev_misc = [50, 100, 0] # Default state
    episode_rewards = 0
    for epoch in range(num_epochs):
        game.new_episode()
        train_scores = []
        global_step = 0
        print("\nEpoch #" + str(epoch + 1))

        for step in trange(steps_per_epoch, leave=False):
            game_state = game.get_state()
            state = preprocess(game_state.screen_buffer)
            misc = game_state.game_variables
            #  reward setting
            health_reward = 10 * (misc[1] - prev_misc[1])
            kill_reward = 1000 * (misc[2] - prev_misc[2])
            ANMO2_reward = (misc[0] - prev_misc[0])
            prev_misc = game_state.game_variables
            action = agent.get_action(state)
            reward = game.make_action(actions[action], frame_repeat) + health_reward + kill_reward + ANMO2_reward
            episode_rewards += reward
            done = game.is_episode_finished()

            if not done:
                next_state = preprocess(game_state.screen_buffer)
            else:
                next_state = np.zeros((1, 160, 120)).astype(np.float32)
                prev_misc = [50, 100, 0]



            agent.append_memory(state, action, reward, next_state, done)

            if global_step > agent.batch_size:
                agent.train()

            if done:
                # train_scores.append(game.get_total_reward())
                train_scores.append(episode_rewards)
                game.new_episode()
                episode_rewards = 0


            global_step += 1

        agent.update_target_net()
        train_scores = np.array(train_scores)
        if len(train_scores) == 0:
            print("Results: mean: %.1f +/- %.1f," % (0, 0),"min: %.1f," % 0, "max: %.1f," % 0)
        else:
            print("Results: mean: %.1f +/- %.1f," % (train_scores.mean(), train_scores.std()),
                  "min: %.1f," % train_scores.min(), "max: %.1f," % train_scores.max())

        # test(game, agent)
        if save_model:
            print("Saving the network weights to:", model_savefile)
            torch.save(agent.q_net, model_savefile)
        print("Total elapsed time: %.2f minutes" % ((time() - start_time) / 60.0))

    game.close()
    return agent, game

class DQNAgent:
    def __init__(self, action_size, memory_size, batch_size, discount_factor, 
                 lr, load_model, epsilon=1, epsilon_decay=0.9996, epsilon_min=0.1):
        self.action_size = action_size
        self.epsilon = epsilon
        self.epsilon_decay = epsilon_decay
        self.epsilon_min = epsilon_min
        self.batch_size = batch_size
        self.discount = discount_factor
        self.lr = lr
        self.memory = deque(maxlen=memory_size)
        self.criterion = torch.nn.MSELoss()

        if load_model:
            print("Loading model from: ", model_savefile)
            self.q_net = torch.load(model_savefile)
            self.target_net = torch.load(model_savefile)
            self.epsilon = self.epsilon_min

        else:
            print("Initializing new model")
            self.q_net = Network(1, action_size, resolution[0]).to(DEVICE)
            self.target_net = Network(1, action_size, resolution[0]).to(DEVICE)

        self.opt = optim.Adam(self.q_net.parameters(), lr=self.lr)

    def get_action(self, state):
        if np.random.uniform() < self.epsilon:
            return random.choice(range(self.action_size))
        else:
            state = np.expand_dims(state, axis=0)
            state = torch.from_numpy(state).float().to(DEVICE)
            action = torch.argmax(self.q_net(state)).item()
            return action

    def update_target_net(self):
        self.target_net.load_state_dict(self.q_net.state_dict())

    def append_memory(self, state, action, reward, next_state, done):
        self.memory.append((state, action, reward, next_state, done))

    def train(self):
        batch = random.sample(self.memory, self.batch_size)
        batch = np.array(batch, dtype=object)
        states = np.stack(batch[:, 0]).astype(float)
        actions = batch[:, 1].astype(int)
        rewards = batch[:, 2].astype(float)
        next_states = np.stack(batch[:, 3]).astype(float)
        dones = batch[:, 4].astype(bool)
        not_dones = ~dones

        row_idx = np.arange(self.batch_size)  # used for indexing the batch

        # value of the next states with double q learning
        # see https://arxiv.org/abs/1509.06461 for more information on double q learning
        with torch.no_grad():
            next_states = torch.from_numpy(next_states).float().to(DEVICE)
            idx = row_idx, np.argmax(self.q_net(next_states).cpu().data.numpy(), 1)
            next_state_values = self.target_net(next_states).cpu().data.numpy()[idx]
            next_state_values = next_state_values[not_dones]

        # this defines y = r + discount * max_a q(s', a)
        q_targets = rewards.copy()
        q_targets[not_dones] += self.discount * next_state_values
        q_targets = torch.from_numpy(q_targets).float().to(DEVICE)

        # this selects only the q values of the actions taken
        idx = row_idx, actions
        states = torch.from_numpy(states).float().to(DEVICE)
        action_values = self.q_net(states)[idx].float().to(DEVICE)

        self.opt.zero_grad()
        td_error = self.criterion(q_targets, action_values) # loss function
        td_error.backward()
        self.opt.step()
        # epsilon is set to explore the environment
        if self.epsilon > self.epsilon_min:
            self.epsilon *= self.epsilon_decay
        else:
            self.epsilon = self.epsilon_min

def summary_writer(actions_length, x=torch.rand(64, 1, 30, 45)):
    with torch.no_grad():
        model = Network(1, actions_length, resolution[0]).to(DEVICE)
    with SummaryWriter(comment='DuelQNet') as net:
        net.add_graph(model, x)

if __name__ == '__main__':
    # Initialize game and actions
    game = create_simple_game()
    n = game.get_available_buttons_size()
    print(game.get_available_buttons())
    actions = [list(a) for a in it.product([0, 1], repeat=n)]

    # Initialize our agent with the set parameters
    agent = DQNAgent(len(actions), lr=learning_rate, batch_size=batch_size,
                     memory_size=replay_memory_size, discount_factor=discount_factor,
                     load_model=load_model)
    # print network
    # summary_writer(len(actions),x=torch.rand(1, 3, resolution[0], resolution[1]).to(DEVICE))
    # Run the training for the set number of epochs
    if not skip_learning:
        agent, game = run(game, agent, actions, num_epochs=train_epochs, frame_repeat=frame_repeat,
                          steps_per_epoch=learning_steps_per_epoch)

        print("======================================")
        print("Training finished. It's time to watch!")

    # Reinitialize the game with window visible
    game.close()
    game.set_window_visible(True)
    game.set_mode(Mode.ASYNC_PLAYER)
    game.init()
    test(game, agent)
    # for _ in range(episodes_to_watch):
    #     game.new_episode()
    #     while not game.is_episode_finished():
    #         state = preprocess(game.get_state().screen_buffer)
    #         best_action_index = agent.get_action(state)
    #
    #         # Instead of make_action(a, frame_repeat) in order to make the animation smooth
    #         game.set_action(actions[best_action_index])
    #         for _ in range(frame_repeat):
    #             game.advance_action()
    #
    #     # Sleep between episodes
    #     sleep(1.0)
    #     score = game.get_total_reward()
    #     print("Total score: ", score)
