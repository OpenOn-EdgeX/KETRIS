"""Observer-owned delta history; reads never request or alter gate credits."""
from ..normalize import CounterPoint,counter_delta,partition_shares
class CounterHistory:
    def __init__(self):self.previous={}
    def update(self,points):
        deltas={key:counter_delta(self.previous.get(key),p) for key,p in points.items()}
        self.previous=dict(points)
        return partition_shares(deltas)
