"""Activity counters use percent units and the whole evaluation interval."""
import math
def interval_mean(samples,start,end,max_gap):
    if end<=start or max_gap<=0:raise ValueError('invalid evaluation interval')
    total=0;weighted=0.0
    for (left,_),(right,value) in zip(samples,samples[1:]):
        if right<=left:raise ValueError('counter timestamps must increase')
        width=max(0,min(end,right)-max(start,left))
        if not width or right-left>max_gap or not math.isfinite(value) or not 0<=value<=100:continue
        total+=width;weighted+=value*width
    if not total:raise ValueError('no valid counter coverage')
    return {'mean_percent':weighted/total,'coverage':total/(end-start),'covered_ns':total,'window_ns':end-start}
def compare_percent(mps,ketris):
    if not all(math.isfinite(x) and 0<=x<=100 for x in (mps,ketris)):raise ValueError('invalid percent')
    return {'mps_percent':mps,'ketris_percent':ketris,'difference_pp':ketris-mps,
            'relative_change_percent':(ketris-mps)/mps*100 if mps else None}
