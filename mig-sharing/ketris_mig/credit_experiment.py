"""Experimental reservation-time allocator, not the deployed v1 time contract.

The resource-time proxy is reported-SM ceiling * worker charged wall time.
It is not measured SM occupancy. The installed daemon never imports this module.
"""
from fractions import Fraction

def sm_time_grants(sm_counts,weights,total_sm,tick_us):
    if type(total_sm) is not int or total_sm<1 or type(tick_us) is not int or not 1<=tick_us<=1000000:
        raise ValueError('positive SM count and bounded tick required')
    if any(type(sm) is not int or not 1<=sm<=total_sm for sm in sm_counts.values()):
        raise ValueError('reported client SM ceiling must be within the MIG')
    if set(weights)-set(sm_counts) or any(type(w) is not int or not 1<=w<=1000 for w in weights.values()):
        raise ValueError('positive integer weights for known clients required')
    remaining=Fraction(total_sm*tick_us)
    pending=dict(weights);assigned={k:Fraction(0) for k in sm_counts}
    while pending:
        denominator=sum(pending.values())
        proposed={k:remaining*w/denominator for k,w in pending.items()}
        saturated=[k for k,v in proposed.items() if v>=sm_counts[k]*tick_us]
        if not saturated:
            assigned.update(proposed);break
        for k in saturated:
            assigned[k]=Fraction(sm_counts[k]*tick_us)
            remaining-=assigned[k];pending.pop(k)
    # Rounding is conservative: resource-time mass never exceeds one MIG tick.
    return {k:int(v//sm_counts[k]) for k,v in assigned.items()}
