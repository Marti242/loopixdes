from uuid import uuid4
from queue import Queue
from typing import Union
from typing import Dict
from typing import List
from typing import Tuple
from typing import Callable
from typing import Optional
from typing import Generator
from inspect import signature
from datetime import datetime

from node import Node
from defaults import *
from client import Client
from model.mail import Mail
from model.packet import Packet

import numpy as np
from tqdm.auto import tqdm
from simpy import Resource
from simpy import Environment
from simpy.util import start_delayed
from torch.utils.tensorboard import SummaryWriter


class Simulator:
    """SIMULATOR"""

    def __init__(
            self,
            traces: List[Mail],
            num_layers: int = 3,
            verbose: bool = False,
            num_providers: int = 3,
            nodes_per_layer: int = 3,
            tensorboard: bool = False,
            num_mix_threads: int = 16,
            plaintext_size: int = 5082,
            num_client_threads: int = 1,
            loop_mix_entropy: bool = True,
            state_sequence_length: int = 2,
            num_provider_threads: int = 64,
            logging_rate: Union[int, float] = 1.0,
            update_rate: Union[int, float] = 100.0,
            udp_model: Callable = default_udp_model,
            warmup_time: Union[int, float] = 2500.0,
            challenger_rate: Union[int, float] = 1.0,
            provider_dist: Optional[np.ndarray] = None,
            client_model: Callable = default_client_model,
            challenger_warmup_time: Union[int, float] = 100.0,
            rng: np.random.RandomState = np.random.RandomState(),
            encryption_model: Callable = default_encryption_model,
            decryption_model: Callable = default_decryption_model,
            init_timestamp: Union[int, float] = 1575158400.901652,
            params: Dict[str, Union[int, float]] = DEFAULT_PARAMS,
    ):
        assert isinstance(traces, list), 'traces must be list'
        assert isinstance(params, dict), 'params must be dict'
        assert isinstance(verbose, bool), 'verbose must be bool'
        assert isinstance(num_layers, int), 'num_layers must be int'
        assert isinstance(tensorboard, bool), 'tensorboard must be bool'
        assert isinstance(warmup_time, NUM), 'warmup_time must be number'
        assert isinstance(num_providers, int), 'num_providers must be int'
        assert isinstance(plaintext_size, int), 'plaintext_size must be int'
        assert isinstance(nodes_per_layer, int), 'nodes_per_layer must be int'
        assert isinstance(init_timestamp, NUM), 'init_timestamp must be number'
        assert isinstance(num_mix_threads, int), 'num_server_threads must be int'
        assert isinstance(challenger_rate, NUM), 'challenger_rate must be number'
        assert isinstance(loop_mix_entropy, bool), 'loop_mix_entropy must be bool'
        assert isinstance(num_client_threads, int), 'num_client_threads must be int'
        assert isinstance(num_provider_threads, int), 'num_server_threads must be int'
        assert isinstance(state_sequence_length, int), 'state_sequence_length must be int'
        assert isinstance(rng, np.random.RandomState), 'rng must be np.random.RandomState'
        assert isinstance(challenger_warmup_time, NUM), 'challenger_warmup_time must be number'

        assert all([isinstance(value, NUM) for value in params.values()]), 'non-number'
        assert all([isinstance(mail, Mail) for mail in traces]), 'every traces item must be Mail'

        assert all([key in DEFAULT_PARAMS for key in params]), 'unknown params'
        assert all([value > 0.0 for value in params.values()]), 'all params must be positive'

        assert num_layers > 0, 'num_layers must be positive'
        assert update_rate > 0.0, 'update_rate must be positive'
        assert warmup_time > 0.0, 'warmup_time must be positive'
        assert logging_rate > 0.0, 'logging_rate must be positive'
        assert num_providers > 0, 'num_providers must be positive'
        assert plaintext_size > 0, 'plaintext_size must be positive'
        assert nodes_per_layer > 0, 'nodes_per_layer must be positive'
        assert challenger_rate > 0.0, 'challenger_rate must be positive'
        assert num_mix_threads > 0, 'num_server_threads must be positive'
        assert init_timestamp >= 0.0, 'init_timestamp must be non-negative'
        assert num_client_threads > 0, 'num_client_threads must be positive'
        assert num_provider_threads > 0, 'num_server_threads must be positive'
        assert state_sequence_length > 0, 'state_sequence_length must be positive'
        assert challenger_warmup_time > 0.0, 'challenger_warmup_time must be positive'

        assert update_rate >= EPS + (state_sequence_length - 1) * logging_rate

        assert isinstance(udp_model, type(lambda: None))
        assert isinstance(client_model, type(lambda: None))
        assert isinstance(encryption_model, type(lambda: None))
        assert isinstance(decryption_model, type(lambda: None))
        assert str(signature(udp_model).return_annotation) == "<class 'float'>"
        assert str(signature(client_model).return_annotation) == "<class 'int'>"
        assert str(signature(encryption_model).return_annotation) == "<class 'float'>"
        assert str(signature(decryption_model).return_annotation) == "<class 'float'>"

        if provider_dist is None:
            provider_dist = np.ones(num_providers) / num_providers

        assert isinstance(provider_dist, np.ndarray), 'provider_dist must be array'
        assert len(provider_dist) == num_providers, 'wrong provider_dist dimensionality'
        assert sum(provider_dist) == 1.0, 'provider_dist must be probability distribution'
        assert all(list(provider_dist > 0.0)), 'provider_dist must be probability distribution'

        self.__rng = rng
        self.__params = params
        self.__traces = traces
        self.__verbose = verbose
        self.__udp_model = udp_model
        self.__num_layers = num_layers
        self.__tensorboard = tensorboard
        self.__warmup_time = warmup_time
        self.__update_rate = update_rate
        self.__client_model = client_model
        self.__logging_rate = logging_rate
        self.__provider_dist = provider_dist
        self.__plaintext_size = plaintext_size
        self.__init_timestamp = init_timestamp
        self.__challenger_rate = challenger_rate
        self.__num_mix_threads = num_mix_threads
        self.__loop_mix_entropy = loop_mix_entropy
        self.__encryption_model = encryption_model
        self.__decryption_model = decryption_model
        self.__num_client_threads = num_client_threads
        self.__num_provider_threads = num_provider_threads
        self.__challenger_warmup_time = challenger_warmup_time

        initial_time = self.__init_timestamp - self.__warmup_time
        initial_time -= self.__challenger_warmup_time

        self.__pki = {}
        self.__users = {}
        self.__providers = []
        self.__challengers = []
        self.__env = Environment(initial_time=initial_time)

        for provider in range(num_providers):
            node_id = f'p{provider}'
            threads = Resource(self.__env, self.__num_provider_threads)

            self.__providers += [f'u-{provider}']
            self.__pki[node_id] = Node(0, threads)
            self.__challengers += [f'u-{provider}'] * 2
            self.__users[f'u-{provider}'] = Client(node_id)

        self.__rng.shuffle(self.__challengers)

        self.__challengers = self.__challengers[:2]

        for layer in range(1, self.__num_layers + 1):
            for node in range(nodes_per_layer):
                node_id = f'm{((layer - 1) * nodes_per_layer + node)}'
                threads = Resource(self.__env, self.__num_mix_threads)

                self.__pki[node_id] = Node(layer, threads)

        self.__per_layer_pki = {}

        for node_id, node in self.__pki.items():
            if node.layer not in self.__per_layer_pki:
                self.__per_layer_pki[node.layer] = []

            self.__per_layer_pki[node.layer] += [node_id]

        for key in DEFAULT_PARAMS:
            if key not in self.__params:
                self.__params[key] = DEFAULT_PARAMS[key]

        for mail in self.__traces:
            providers = self.__per_layer_pki[0]
            provider = self.__rng.choice(providers, p=self.__provider_dist)

            if mail.sender not in self.__users:
                threads = Resource(self.__env, self.__num_client_threads)
                self.__users[mail.sender] = Client(provider, threads, Queue())
            elif self.__users[mail.sender].threads is None:
                threads = Resource(self.__env, self.__num_client_threads)
                self.__users[mail.sender].threads = threads
                self.__users[mail.sender].payload_queue = Queue()

            if mail.receiver not in self.__users:
                self.__users[mail.receiver] = Client(provider)

            delay = mail.time + self.__warmup_time
            delay += self.__challenger_warmup_time
            start_delayed(self.__env, self.__payload_to_sphinx(mail), delay)

        for of_type in ['LOOP', 'DROP', 'PAYLOAD', 'LOOP_MIX']:
            self.__env.process(self.__decoy_worker(of_type))

        delay = self.__warmup_time
        start_delayed(self.__env, self.__challenge_worker(0), delay)
        start_delayed(self.__env, self.__challenge_worker(1), delay)

        if self.__verbose:
            self.__env.process(self.__logging_wrapper())

        # TODO metrics - bandwidth
        total = self.__warmup_time + self.__challenger_warmup_time

        self.__writer = None
        self.__latency = 0.0
        self.__bandwidth = 0
        self.__active_users = {}
        self.__latency_tracker = {}
        self.__num_payload_delivered = 0
        self.__run_id = f'run_{uuid4().hex}'
        self.__logging_interval_start = initial_time
        self.__termination_event = self.__env.event()
        self.__state_sequence_length = state_sequence_length
        self.__state_buffer = Queue(maxsize=state_sequence_length)
        self.__pbar = tqdm(
            total=total, bar_format=BAR_FORMAT, disable=not verbose, position=0
        )

        if self.__tensorboard:
            self.__writer = SummaryWriter()

    def __get_num_workers(self) -> int:
        num_workers = self.__client_model(self.__env.now)
        warmup_done = self.__init_timestamp - self.__challenger_warmup_time

        if self.__env.now > warmup_done:
            return max(num_workers, len(self.__active_users) + 3)

        return max(num_workers, 1)

    def __sample_sender(self, payload: bool = False) -> str:
        if not payload:
            return self.__rng.choice(self.__providers, p=self.__provider_dist)

        num_workers = self.__get_num_workers()
        left_p = 1 - len(self.__active_users) / num_workers

        dim = len(self.__active_users) + len(self.__providers)
        distribution = np.zeros(dim)
        distribution[:len(self.__active_users)] = 1 / num_workers
        distribution[len(self.__active_users):] = self.__provider_dist * left_p
        possible_senders = list(self.__active_users.keys()) + self.__providers

        return self.__rng.choice(possible_senders, p=distribution)

    def __gen_packet(
            self,
            sender: str,
            msg_id: str,
            of_type: str,
            split: int = 0,
            num_splits: int = 1,
            receiver: Optional[str] = None
    ) -> Packet:
        if of_type == 'LOOP_MIX':
            path = []
            layer = self.__pki[sender].layer

            for next_layer in range(layer + 1, len(self.__per_layer_pki)):
                path += [self.__rng.choice(self.__per_layer_pki[next_layer])]

            for next_layer in range(layer):
                path += [self.__rng.choice(self.__per_layer_pki[next_layer])]

            dest = sender
            path += [sender]
        else:
            path = []
            dest = None
            receiver_provider = None
            sender_provider = self.__users[sender].provider

            for layer in range(1, len(self.__per_layer_pki)):
                path += [self.__rng.choice(self.__per_layer_pki[layer])]

            if of_type == 'PAYLOAD':
                dest = receiver
                receiver_provider = self.__users[receiver].provider
            elif of_type == 'DROP':
                receiver_provider = self.__rng.choice(self.__per_layer_pki[0])
                dest = receiver_provider
            elif of_type == 'LOOP':
                dest = sender
                receiver_provider = sender_provider

            path = [sender_provider] + path + [receiver_provider]

        expected_delay = 0.0
        routing = [(path[0], -1.0)]

        for node in path[1:]:
            delay = self.__rng.exponential(self.__params[f'DELAY'])
            routing += [(node, delay)]
            expected_delay += routing[-1][1]

        routing += [(dest, -1.0)]
        args = (routing, split, msg_id, sender, of_type, num_splits)
        args += (expected_delay, )

        return Packet(*args)

    def __payload_to_sphinx(self, mail: Mail) -> Generator:
        of_type = 'PAYLOAD'
        sender = mail.sender
        msg_id = uuid4().hex
        receiver = mail.receiver
        num_splits = int(np.ceil(mail.size / self.__plaintext_size))

        for split_id in range(num_splits):
            args = (sender, msg_id, of_type, split_id, num_splits, receiver)
            packet = self.__gen_packet(*args)
            request = self.__users[sender].threads.request()

            yield request

            runtime = self.__encryption_model(**self.__dict__)

            yield self.__env.timeout(runtime)

            self.__users[sender].threads.release(request)
            self.__users[sender].payload_queue.put(packet)

            if sender not in self.__active_users:
                self.__active_users[sender] = 1
            else:
                self.__active_users[sender] += 1

    def __challenge_worker(self, num: int) -> Generator:
        last_time = self.__env.now

        while True:
            delay = self.__rng.exponential(1 / self.__params['PAYLOAD'])

            yield self.__env.timeout(delay)

            if self.__env.now >= last_time + 1 / self.__challenger_rate:
                self.__env.process(self.__send_packet(f'CHALLENGE_{num}'))
                last_time += 1 / self.__challenger_rate
            else:
                self.__env.process(self.__send_packet('PAYLOAD'))

    def __decoy_worker(self, of_type: str) -> Generator:
        while True:
            param = 1 / self.__params[of_type]
            delay = self.__rng.exponential(param)

            yield self.__env.timeout(delay)
            self.__env.process(self.__send_packet(of_type))

    def __send_packet(
            self,
            of_type: str,
            data: Optional[Packet] = None
    ) -> Generator:
        if of_type == 'LOOP_MIX':
            sender = self.__rng.choice(list(self.__pki.keys()))
        elif of_type[:-1] == 'CHALLENGE_':
            sender = self.__challengers[int(of_type[-1])]
        elif of_type != 'DELAY':
            sender = self.__sample_sender(of_type == 'PAYLOAD')
        else:
            sender = data.sender

        if of_type == 'PAYLOAD':
            user_queue = self.__users[sender].payload_queue

            if user_queue is not None and not user_queue.empty():
                data = user_queue.get()
                self.__active_users[sender] -= 1

                if self.__active_users[sender] == 0:
                    del self.__active_users[sender]

        if data is None:
            actual_type = of_type

            if of_type in ['PAYLOAD', 'CHALLENGE_0', 'CHALLENGE_1']:
                actual_type = 'DROP'

            msg_id = uuid4().hex
            data = self.__gen_packet(sender, msg_id, actual_type)

            if of_type == 'CHALLENGE_0':
                data.dist = np.array([1.0, 0.0, 0.0])
            elif of_type == 'CHALLENGE_1':
                data.dist = np.array([0.0, 1.0, 0.0])

        if of_type == 'LOOP_MIX':
            msg_id = data.msg_id
            expected_delay = data.expected_delay
            args = (self.__env.now, expected_delay)
            self.__pki[sender].sending_time[msg_id] = args

        actual_type = data.of_type

        if of_type == 'DELAY' and actual_type != 'LOOP_MIX':
            data.dist = self.__pki[sender].prob_sum / self.__pki[sender].n
            data.dist = data.dist / sum(data.dist)
            self.__pki[sender].n -= 1
            self.__pki[sender].prob_sum = data.dist * self.__pki[sender].n

            is_non_zero = data.dist[0] > 0.0 and data.dist[1] > 0.0
            is_measure_time = self.__env.now >= self.__init_timestamp
            is_last_layer = self.__pki[sender].layer == self.__num_layers

            if is_non_zero and is_last_layer and is_measure_time:
                epsilon = abs(np.log2(data.dist[0] / data.dist[1]))
                self.__pki[sender].epsilon = epsilon

        if of_type == 'PAYLOAD' and actual_type == 'PAYLOAD':
            msg_id = data.msg_id
            num_splits = data.num_splits

            if msg_id not in self.__latency_tracker:
                self.__latency_tracker[msg_id] = [num_splits, self.__env.now]

        sender_request = None
        receiver = data.path[0][0]
        receiver_request = self.__pki[receiver].threads.request()
        requests = receiver_request

        if of_type in ['LOOP_MIX', 'DELAY']:
            sender_request = self.__pki[sender].threads.request()
            requests = self.__env.all_of((receiver_request, sender_request))
        if of_type == 'PAYLOAD' and actual_type == 'PAYLOAD':
            sender_request = self.__users[sender].threads.request()
            requests = self.__env.all_of((receiver_request, sender_request))

        yield requests

        runtime = self.__udp_model(**self.__dict__)

        yield self.__env.timeout(runtime)
        self.__pki[receiver].threads.release(receiver_request)

        if of_type in ['LOOP_MIX', 'DELAY']:
            self.__pki[sender].threads.release(sender_request)
        elif of_type == 'PAYLOAD' and actual_type == 'PAYLOAD':
            self.__users[sender].threads.release(sender_request)

        yield self.__env.process(self.__process_packet(of_type, data))

    def __process_packet(self, of_type: str, data: Packet) -> Generator:
        actual_type = data.of_type
        loop_mix_entropy = of_type == 'LOOP_MIX' and self.__loop_mix_entropy

        if of_type == 'DELAY' or loop_mix_entropy:
            sender = data.sender

            if loop_mix_entropy:
                self.__pki[sender].l_t += 1

            self.__pki[sender].update_entropy()

        receiver = data.path[0][0]
        request = self.__pki[receiver].threads.request()

        yield request

        runtime = self.__decryption_model(**self.__dict__)

        yield self.__env.timeout(runtime)
        self.__pki[receiver].threads.release(request)

        data.path = data.path[1:]
        data.sender = receiver

        if data.path[0][1] >= 0.0:
            self.__pki[receiver].k_t += 1

            if actual_type != 'LOOP_MIX':
                self.__pki[receiver].n += 1
                self.__pki[receiver].prob_sum += data.dist

            delay = data.path[0][1]

            yield start_delayed(
                self.__env,
                self.__send_packet(f'DELAY', data),
                delay
            )
        else:
            self.__postprocess(data)

    def __postprocess(self, packet: Packet):
        msg_id = packet.msg_id
        of_type = packet.of_type

        if of_type == 'PAYLOAD':
            self.__latency_tracker[msg_id][0] -= 1

        if of_type == 'PAYLOAD' and self.__latency_tracker[msg_id][0] == 0:
            self.__num_payload_delivered += 1
            self.__latency = self.__env.now - self.__latency_tracker[msg_id][1]

            if self.__num_payload_delivered == len(self.__traces):
                self.__termination_event.succeed()

        elif of_type == 'LOOP_MIX':
            node_id = packet.path[0][0]

            self.__pki[node_id].postprocess(self.__env.now, msg_id)

    def __capture_state(self) -> np.ndarray:
        if self.__state_buffer.full():
            self.__state_buffer.get()

        performance_metrics = [[], [], [], []]

        for node_id, node in self.__pki.items():
            performance_metrics[1] += [node.loop_mix_latency]

            if node.layer > 0:
                performance_metrics[2] += [node.h_t]

                if node.layer == self.__num_layers:
                    performance_metrics[0] += [node.epsilon]
            else:
                performance_metrics[3] += [node.h_t]

        summary_stats = np.zeros(20)
        summary_stats[0] = self.__env.now
        summary_stats[1] = self.__get_num_workers()
        summary_stats[2] = self.__latency
        summary_stats[3] = self.__bandwidth

        for i in range(4):
            summary_stats[4 * i + 4] = np.min(performance_metrics[i])
            summary_stats[4 * i + 5] = np.mean(performance_metrics[i])
            summary_stats[4 * i + 6] = np.max(performance_metrics[i])
            summary_stats[4 * i + 7] = np.std(performance_metrics[i])

        self.__state_buffer.put(summary_stats)

        return summary_stats

    def __logging_wrapper(self, step_end: Optional[float] = None) -> Generator:
        counter = 0
        sequence_len = self.__state_sequence_length

        if step_end is not None:
            capture_start = step_end - (sequence_len - 1) * self.__logging_rate
            timeout = capture_start - self.__env.now - EPS

            yield self.__env.timeout(timeout)
            self.__capture_state()

            counter += 1

        non_verbose = step_end is not None and counter < sequence_len

        while self.__verbose or non_verbose:
            yield self.__env.timeout(self.__logging_rate)

            stats = self.__capture_state()

            if self.__verbose:
                update = self.__env.now - self.__logging_interval_start

                self.__pbar.update(update - self.__pbar.n)

                display_stats = list(stats)
                timestamp = datetime.fromtimestamp(stats[0])
                display_stats[0] = timestamp.strftime('%Y-%m-%d, %A, %H:%M:%S')

                for idx, desc in enumerate(LOG_STR.format(*display_stats).split('\n')):
                    self.__pbar.display(desc, idx + 1)

            if self.__tensorboard:
                args = (self.__run_id, dict(zip(LABELS, stats[IDX])), stats[0])

                self.__writer.add_scalars(*args)

            counter += 1

    def warmup(self):
        self.__env.run(self.__init_timestamp)

        if self.__verbose:
            update = self.__warmup_time + self.__challenger_warmup_time

            self.__pbar.update(update - self.__pbar.n)

    def update_parameters(self, params: np.ndarray):
        assert params.shape == 5, 'wrong number of parameters'
        assert np.all(params > 0.0), 'out of bound parameters'

        self.__params = dict(zip(list(DEFAULT_PARAMS.keys()), params))

    def simulation_step(
            self,
            until: Optional[Union[float, int]] = None
    ) -> Tuple[np.ndarray, np.ndarray, bool, Dict]:
        assert not self.__termination_event.triggered, 'episode ended already, init new simulator'
        assert until is None or (isinstance(until, NUM) and until > 0.0), 'until must be positive'

        if until is None:
            until = self.__update_rate

        if not self.__verbose:
            self.__env.process(self.__logging_wrapper(self.__env.now + until))
        else:
            self.__pbar.reset(until)

        self.__logging_interval_start = self.__env.now
        timeout = self.__env.timeout(until)
        until_event = self.__env.any_of((self.__termination_event, timeout))

        self.__env.run(until_event)

        if self.__verbose:
            self.__pbar.update(until - self.__pbar.n)

        if self.__termination_event.triggered:
            self.__pbar.close()

        state = np.zeros((self.__state_sequence_length, 32))

        state[:, :5] = np.array(list(self.__params.values()))
        state[:, 5] = self.__num_layers
        state[:, 6] = len(self.__providers)
        state[:, 7] = len(self.__per_layer_pki[1])
        state[:, 8] = self.__plaintext_size

        for step in range(self.__state_sequence_length):
            stats = self.__state_buffer.get()
            shifted_date = 2 * np.pi / (24 * 60 * 60)
            shifted_date *= (stats[0] - REF_TIMESTAMP)
            state[step, 9] = np.sin(shifted_date)
            state[step, 10] = np.cos(shifted_date)
            state[step, 11] = np.sin(shifted_date / 7)
            state[step, 12] = np.cos(shifted_date / 7)
            state[step, 13:] = stats[1:]

            self.__state_buffer.put(stats)

        reward = state[:, 15:]
        done = self.__termination_event.triggered

        return state, reward, done, {}
