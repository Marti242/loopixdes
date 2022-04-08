from numpy import log2
from numpy import array

from simpy import Resource


class Node:
    """NODE"""

    def __init__(self, layer: int, threads: Resource):
        self.layer = layer

        self.k_t = 0
        self.l_t = 0
        self.h_t = 0.0

        self.n = 0
        self.epsilon = 0.0
        self.prob_sum = array([0.0, 0.0, 0.0])

        self.threads = threads
        self.sending_time = {}
        self.loop_mix_latency = 0.0

    def postprocess(self, timestamp: float, msg_id: str):
        all_times = self.sending_time.values()
        all_times = [timestamp - send_time for send_time, _ in all_times]
        max_latency = max(all_times)
        self.loop_mix_latency = max_latency
        expected_delay = self.sending_time[msg_id][1]
        msg_latency = timestamp - self.sending_time[msg_id][0]

        assert msg_latency >= expected_delay, 'MESSAGE RECEIVED TOO EARLY'

        del self.sending_time[msg_id]

    def update_entropy(self,) -> float:
        denominator = self.k_t + self.l_t
        self.h_t = self.l_t * self.h_t / denominator

        if self.k_t != 0:
            self.h_t += self.k_t * log2(self.k_t) / denominator
            self.h_t -= self.k_t / denominator * log2(self.k_t / denominator)

        if self.l_t != 0:
            self.h_t -= self.l_t / denominator * log2(self.l_t / denominator)

        self.l_t = self.l_t + self.k_t - 1
        self.k_t = 0

        return self.h_t
