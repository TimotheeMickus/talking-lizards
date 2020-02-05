import torch
import torch.nn as nn
from torch.utils.tensorboard import SummaryWriter
import tqdm

from sender import Sender
from receiver import Receiver
from senderReceiver import SenderReceiver
from utils import show_imgs, max_normalize_, to_color

from config import *

class Progress:
    def __init__(self, simple_display, steps_per_epoch, epoch):
        self.simple_display = simple_display
        self.steps_per_epoch = steps_per_epoch
        self.epoch = epoch

    def __enter__(self):
        if(self.simple_display): self.i = 0
        else: self.pbar = tqdm.tqdm(total=self.steps_per_epoch, postfix={"R": 0.0}, unit="B", desc=("Epoch %i" % self.epoch)) # Do not forget to close it at the end

        return self

    def update(self, r):
        if(self.simple_display):
            print(('%i/%i - R: %f' % (self.i, self.steps_per_epoch, r)), flush=True)
            self.i += 1
        else:
            self.pbar.set_postfix({"R" : r}, refresh=False)
            self.pbar.update()

    def __exit__(self, type, value, traceback):
        if(not self.simple_display): self.pbar.close()

class AliceBob(nn.Module):
    def __init__(self, shared=False):
        nn.Module.__init__(self)

        if(shared):
            senderReceiver = SenderReceiver()
            self.sender = senderReceiver.sender
            self.receiver = senderReceiver.receiver
        else:
            self.sender = Sender()
            self.receiver = Receiver()

    @staticmethod
    def _forward(batch, sender, receiver):
        sender_outcome = sender(batch.alice_input)
        receiver_outcome = receiver(batch.bob_input, *sender_outcome.action)

        return sender_outcome, receiver_outcome

    def forward(self, batch):
        """
        Input:
            `batch` is a Batch (a kind of named tuple); 'alice_input' is a tensor of shape [BATCH_SIZE, *IMG_SHAPE] and 'bob_input' is a tensor of shape [BATCH_SIZE, K, *IMG_SHAPE]
        Output:
            `sender_outcome`, sender.Outcome
            `receiver_outcome`, receiver.Outcome
        """
        return self._forward(batch, self.sender, self.receiver)

    def compute_rewards(self, sender_action, receiver_action, running_avg_success, chance_perf):
        """
            returns the reward as well as the success for each element of a batch
        """
        successes = (receiver_action == 0).float() # by design, the first image is the target

        rewards = successes

        if(args.penalty > 0.0):
            msg_lengths = sender_action[1].view(-1).float() # Float casting could be avoided if we upgrade torch to 1.3.1; cf. https://github.com/pytorch/pytorch/issues/9515 (I believe)
            length_penalties = 1.0 - (1.0 / (1.0 + args.penalty * msg_lengths)) # Equal to 0 when `args.penalty` is set to 0, increases to 1 with the length of the message otherwise

            # TODO J'ai peur que ce système soit un peu trop basique et qu'il encourage le système à être sous-performant - qu'on puisse obtenir plus de reward en faisant exprès de se tromper.
            if(args.adaptative_penalty):
                improvement_factor = (running_avg_success - chance_perf) / (1 - chance_perf) # Equals 0 when running average equals chance performance, reachs 1 when running average reaches 1
                length_penalties = (length_penalties * min(0.0, improvement_factor))

            rewards = (rewards - length_penalties)

        return (rewards, successes)

    def compute_log_prob(self, sender_log_prob, receiver_log_prob):
        """
        Input:
            `sender_log_prob`, log probs for sender policy
            `receiver_log_prob`, log prob for receiver policy
        Output:
            \bigg(\sum \limits_{l=1}^L \log p_{\pi^s}(m^l_t|m^{<l}_t, u) + \log p_{\pi^L}(u_{t'}|z, U) \bigg)
        """
        log_prob = sender_log_prob.sum(dim=1) + receiver_log_prob
        return log_prob

    def test_visualize(self, data_iterator):
        self.eval() # Sets the model in evaluation mode; good idea or not?

        batch_size = 2 # Maybe it would just be simpler to work with multiple batches of size 1
        batch = data_iterator.get_batch(batch_size)

        batch.alice_input.requires_grad = True
        batch.bob_input.requires_grad = True

        pseudo_optimizer = torch.optim.Optimizer(list(self.parameters()) + [batch.alice_input, batch.bob_input], {}) # I'm defining this only for its `zero_grad` method (but maybe we won't need it)

        sender_outcome, receiver_outcome = self(batch)

        pseudo_optimizer.zero_grad()

        _COLOR, _INTENSITY = range(2)
        def process(t, dim, mode):
            if(mode == _COLOR):
                t = max_normalize(t, dim=dim, abs_val=True) # Normalises each image
                t *= 0.5
                t += 0.5

                return t
            elif(mode == _INTENSITY):
                t = t.abs()
                t = t.max(dim).values # Max over the colour channel
                max_normalize_(t, dim=dim, abs_val=False) # Normalises each image
                return to_color(t, dim)

        mode = _INTENSITY

        # Alice's part
        sender_outcome.log_prob.sum().backward()
        sender_part = batch.alice_input.grad.detach()

        sender_part = process(sender_part, 1, mode)

        # Bob's part
        receiver_outcome.scores.sum().backward()
        receiver_part = batch.bob_input.grad.detach()

        receiver_part = process(receiver_part, 2, mode)

        imgs = []
        img_per_batch = batch.bob_input.shape[1]
        for i in range(batch_size):
            imgs.append(batch.alice_input[i].detach())
            imgs.append(sender_part[i])
            for j in range(img_per_batch):
                imgs.append(batch.bob_input[i][j].detach())
                imgs.append(receiver_part[i][j])
        show_imgs(imgs, nrow=(2 * (1 + img_per_batch)))

    def train_epoch(self, data_iterator, optim, epoch=1, steps_per_epoch=1000, event_writer=None):
        """
            Model training function
            Input:
                `data_iterator`, an infinite iterator over (batched) data
                `optim`, the optimizer
            Optional arguments:
                `epoch`: epoch number to display in progressbar
                `steps_per_epoch`: number of steps for epoch
                `event_writer`: tensorboard writer to log evolution of values
        """
        self.train() # Sets the model in training mode

        with Progress(SIMPLE_DISPLAY, steps_per_epoch, epoch) as pbar:
            total_reward = 0.0 # sum of the rewards since the beginning of the epoch
            total_success = 0.0 # sum of the successes since the beginning of the epoch
            total_items = 0 # number of training instances since the beginning of the epoch
            running_avg_reward = 0.0
            running_avg_success = 0.0
            start_i = ((epoch - 1) * steps_per_epoch) + 1 # (the first epoch is numbered 1, and the first iteration too)
            end_i = start_i + steps_per_epoch

            for i, batch in zip(range(start_i, end_i), data_iterator):
                optim.zero_grad()
                sender_outcome, receiver_outcome = self(batch)

                chance_perf = (1 / batch.bob_input.shape[1]) # The chance performance is 1 over the number of images shown to Bob
                (rewards, successes) = self.compute_rewards(sender_outcome.action, receiver_outcome.action, running_avg_success, chance_perf)
                log_prob = self.compute_log_prob(sender_outcome.log_prob, receiver_outcome.log_prob)
                loss = -(rewards * log_prob)

                loss = loss.mean()
                # entropy penalties
                loss = loss - (BETA_SENDER * sender_outcome.entropy.mean())
                loss = loss - (BETA_RECEIVER * receiver_outcome.entropy.mean())

                # backprop
                loss.backward()

                # Gradient clipping and scaling
                if((CLIP_VALUE is not None) and (CLIP_VALUE > 0)): torch.nn.utils.clip_grad_value_(self.parameters(), CLIP_VALUE)
                if((SCALE_VALUE is not None) and (SCALE_VALUE > 0)): torch.nn.utils.clip_grad_norm_(self.parameters(), SCALE_VALUE)

                optim.step()

                avg_reward = rewards.mean().item() # average reward of the batch
                avg_success = successes.mean().item() # average success of the batch
                avg_msg_length = sender_outcome.action[1].float().mean().item() # average message length of the batch

                # updates running average reward
                total_reward += rewards.sum().item()
                total_success += successes.sum().item()
                total_items += batch.size
                running_avg_reward = total_reward / total_items
                running_avg_success = total_success / total_items

                pbar.update(running_avg_success)

                # logs some values
                if(event_writer is not None):
                    number_ex_seen = i * BATCH_SIZE
                    event_writer.add_scalar('train/reward', avg_reward, number_ex_seen)
                    event_writer.add_scalar('train/success', avg_success, number_ex_seen)
                    event_writer.add_scalar('train/loss', loss.item(), number_ex_seen)
                    event_writer.add_scalar('train/msg_length', avg_msg_length, number_ex_seen)
                    if DEBUG_MODE:
                        median_grad = torch.cat([p.grad.view(-1).detach() for p in self.parameters()]).abs().median().item()
                        mean_grad = torch.cat([p.grad.view(-1).detach() for p in self.parameters()]).abs().mean().item()
                        #min_grad = torch.cat([p.grad.view(-1).detach() for p in self.parameters()]).abs().min().item()
                        max_grad = torch.cat([p.grad.view(-1).detach() for p in self.parameters()]).abs().max().item()
                        mean_norm_grad = torch.stack([p.grad.view(-1).detach().data.norm(2.) for p in self.parameters()]).mean().item()
                        max_norm_grad = torch.stack([p.grad.view(-1).detach().data.norm(2.) for p in self.parameters()]).max().item()
                        event_writer.add_scalar('train/median_grad', median_grad, number_ex_seen)
                        event_writer.add_scalar('train/mean_grad', mean_grad, number_ex_seen)
                        #event_writer.add_scalar('train/min_grad', min_grad, number_ex_seen)
                        event_writer.add_scalar('train/max_grad', max_grad, number_ex_seen)
                        event_writer.add_scalar('train/mean_norm_grad', mean_norm_grad, number_ex_seen)
                        event_writer.add_scalar('train/max_norm_grad', max_norm_grad, number_ex_seen)

        self.eval()
