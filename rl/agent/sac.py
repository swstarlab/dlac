import os
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F

from agent.buffers import *
from agent.networks import *


class Agent(object):
   """
   An implementation of Soft Actor-Critic (SAC) algorithm.
   https://arxiv.org/abs/1801.01290
   """

   def __init__(self,
                env,
                args,
                device,
                obs_dim,
                act_dim,
                act_limit,
                steps=0,
                gamma=0.99,
                alpha=0.2,
                hidden_sizes=(300,300),
                buffer_size=int(1e6),
                batch_size=100,
                actor_lr=1e-3,
                qf_lr=1e-3,
                eval_mode=False,
                actor_losses=list(),
                qf1_losses=list(),
                qf2_losses=list(),
                alpha_losses=list(),
                logger=dict(),
   ):

      self.env = env
      self.args = args
      self.device = device
      self.obs_dim = obs_dim
      self.act_dim = act_dim
      self.act_limit = act_limit
      self.steps = steps 
      self.gamma = gamma
      self.alpha = alpha
      self.hidden_sizes = hidden_sizes
      self.buffer_size = buffer_size
      self.batch_size = batch_size
      self.actor_lr = actor_lr
      self.qf_lr = qf_lr
      self.eval_mode = eval_mode
      self.actor_losses = actor_losses
      self.qf1_losses = qf1_losses
      self.qf2_losses = qf2_losses
      self.alpha_losses = alpha_losses
      self.logger = logger

      # Main network
      self.actor = ReparamGaussianPolicy(self.obs_dim, self.act_dim, hidden_sizes=self.hidden_sizes, 
                                                                     action_scale=self.act_limit).to(self.device)
      self.qf1 = FlattenMLP(self.obs_dim+self.act_dim, 1, hidden_sizes=self.hidden_sizes).to(self.device)
      self.qf2 = FlattenMLP(self.obs_dim+self.act_dim, 1, hidden_sizes=self.hidden_sizes).to(self.device)
      # Target network
      self.qf1_target = FlattenMLP(self.obs_dim+self.act_dim, 1, hidden_sizes=self.hidden_sizes).to(self.device)
      self.qf2_target = FlattenMLP(self.obs_dim+self.act_dim, 1, hidden_sizes=self.hidden_sizes).to(self.device)
      
      # Initialize target parameters to match main parameters
      self.hard_target_update(self.qf1, self.qf1_target)
      self.hard_target_update(self.qf2, self.qf2_target)

      # If ture, set the trained embedding model 
      if self.args.mode == 'embed':
         embedding_model_path = os.path.join('../embedding/asset/' + str(self.args.path))
         embedding_model = torch.load(embedding_model_path, map_location=self.device)
         self.model = DynamicsEmbedding(self.obs_dim, self.obs_dim, self.act_dim).to(self.device)
         self.model.load_state_dict(embedding_model)

      # Create optimizers
      self.actor_optimizer = optim.Adam(self.actor.parameters(), lr=self.actor_lr)
      self.qf1_optimizer = optim.Adam(self.qf1.parameters(), lr=self.qf_lr)
      self.qf2_optimizer = optim.Adam(self.qf2.parameters(), lr=self.qf_lr)
      
      # Experience buffer
      self.replay_buffer = ReplayBuffer(self.obs_dim, self.act_dim, self.buffer_size, self.device)

   def hard_target_update(self, main, target):
      target.load_state_dict(main.state_dict())

   def soft_target_update(self, main, target, tau=0.005):
      for main_param, target_param in zip(main.parameters(), target.parameters()):
         target_param.data.copy_(tau*main_param.data + (1.0-tau)*target_param.data)

   def train_model(self):
      batch = self.replay_buffer.sample(self.batch_size)
      obs1 = batch['obs1']
      obs2 = batch['obs2']
      acts = batch['acts']
      rews = batch['rews']
      done = batch['done']

      if 0: # Check shape of experiences
         print("obs1", obs1.shape)
         print("obs2", obs2.shape)
         print("acts", acts.shape)
         print("rews", rews.shape)
         print("done", done.shape)

      # Prediction π(s), logπ(s), π(s'), logπ(s'), Q1(s,a), Q2(s,a)
      _, pi, log_pi = self.actor(obs1)
      _, next_pi, next_log_pi = self.actor(obs2)
      q1 = self.qf1(obs1, acts).squeeze(1)
      q2 = self.qf2(obs1, acts).squeeze(1)

      # Min Double-Q: min(Q1(s,π(s)), Q2(s,π(s))), min(Q1‾(s',π(s')), Q2‾(s',π(s')))
      min_q_pi = torch.min(self.qf1(obs1, pi), self.qf2(obs1, pi)).squeeze(1).to(self.device)
      min_q_next_pi = torch.min(self.qf1_target(obs2, next_pi), 
                                self.qf2_target(obs2, next_pi)).squeeze(1).to(self.device)

      # Targets for Q and V regression
      v_backup = min_q_next_pi - self.alpha*next_log_pi
      q_backup = rews + self.gamma*(1-done)*v_backup
      q_backup.to(self.device)

      if 0: # Check shape of prediction and target
         print("log_pi", log_pi.shape)
         print("next_log_pi", next_log_pi.shape)
         print("q1", q1.shape)
         print("q2", q2.shape)
         print("min_q_pi", min_q_pi.shape)
         print("min_q_next_pi", min_q_next_pi.shape)
         print("q_backup", q_backup.shape)

      # Soft actor-critic losses
      actor_loss = (self.alpha*log_pi - min_q_pi).mean()
      qf1_loss = F.mse_loss(q1, q_backup.detach())
      qf2_loss = F.mse_loss(q2, q_backup.detach())

      # Update two Q network parameter
      self.qf1_optimizer.zero_grad()
      qf1_loss.backward()
      self.qf1_optimizer.step()

      self.qf2_optimizer.zero_grad()
      qf2_loss.backward()
      self.qf2_optimizer.step()
      
      # Update actor network parameter
      self.actor_optimizer.zero_grad()
      actor_loss.backward()
      self.actor_optimizer.step()

      # Polyak averaging for target parameter
      self.soft_target_update(self.qf1, self.qf1_target)
      self.soft_target_update(self.qf2, self.qf2_target)
      
      # Save losses
      self.actor_losses.append(actor_loss.item())
      self.qf1_losses.append(qf1_loss.item())
      self.qf2_losses.append(qf2_loss.item())

   def run(self, max_step):
      step_number = 0
      total_reward = 0.

      obs = self.env.reset()
      done = False

      # Keep interacting until agent reaches a terminal state.
      while not (done or step_number == max_step):
         self.steps += 1
         
         if self.eval_mode:
            if self.args.mode == 'raw':
               action, _, _ = self.actor(torch.Tensor(obs).to(self.device))
               action = action.detach().cpu().numpy()
            elif self.args.mode == 'embed':
               z_obs = self.model.encode(torch.Tensor(obs).to(self.device))[0]
               z_obs = z_obs.detach().cpu().numpy()
               action, _, _ = self.actor(torch.Tensor(z_obs).to(self.device))
               action = action.detach().cpu().numpy()
            next_obs, reward, done, _ = self.env.step(action)
         else:
            if self.args.mode == 'raw':
               # Collect experience (s, a, r, s') using some policy
               _, action, _ = self.actor(torch.Tensor(obs).to(self.device))
               action = action.detach().cpu().numpy()
               
               next_obs, reward, done, _ = self.env.step(action)

               # Add experience to replay buffer
               self.replay_buffer.add(obs, action, reward, next_obs, done)
               
               # Start training when the number of experience is greater than batch size
               if self.steps > self.batch_size:
                  self.train_model()
            elif self.args.mode == 'embed':
               # Collect experience (z_s, a, r, z_s') using some policy
               z_obs = self.model.encode(torch.Tensor(obs).to(self.device))[0]
               z_obs = z_obs.detach().cpu().numpy()

               _, action, _ = self.actor(torch.Tensor(z_obs).to(self.device))
               action = action.detach().cpu().numpy()

               next_obs, reward, done, _ = self.env.step(action)

               z_next_obs = self.model.encode(torch.Tensor(next_obs).to(self.device))[0]
               z_next_obs = z_next_obs.detach().cpu().numpy()

               # Add experience to replay buffer
               self.replay_buffer.add(z_obs, action, reward, z_next_obs, done)
               
               # Start training when the number of experience is greater than batch size
               if self.steps > self.batch_size:
                  self.train_model()

         total_reward += reward
         step_number += 1
         obs = next_obs
      
      # Save logs
      self.logger['LossPi'] = round(np.mean(self.actor_losses), 4)
      self.logger['LossQ1'] = round(np.mean(self.qf1_losses), 4)
      self.logger['LossQ2'] = round(np.mean(self.qf2_losses), 4)
      return step_number, total_reward
