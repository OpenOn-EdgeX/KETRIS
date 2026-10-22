"""Opt-in phase policy for calibrated KETRIS experimental workers."""
class PhasePolicy:
    def __init__(self, total_tpcs, prefill_tpcs, decode_tpcs, backgrounds, stale_ns, *, share_background=False):
        if (not all(isinstance(x,int) and x>0 for x in
                    (total_tpcs,prefill_tpcs,decode_tpcs,backgrounds,stale_ns)) or
            max(prefill_tpcs,decode_tpcs)+backgrounds>total_tpcs):
            raise ValueError('invalid phase partition configuration')
        self.total=total_tpcs;self.prefill=prefill_tpcs;self.decode=decode_tpcs
        self.backgrounds=backgrounds;self.stale_ns=stale_ns
        self.share_background=share_background

    def after_drain(self, read_phase, drain, clock):
        drain()
        return self.decide(*read_phase(),clock())

    def decide(self, phase, observed_ns, now_ns):
        stale=phase not in ('idle','prefill','decode') or not 0<=now_ns-observed_ns<=self.stale_ns
        if stale:phase='idle'
        active=self.prefill if phase=='prefill' else self.decode if phase=='decode' else 0
        ranges={'A':(0,active or self.prefill)}
        if self.share_background:
            ranges.update({'B'+str(i+1):(active,self.total-active) for i in range(self.backgrounds)})
            return {'ranges':ranges,'pause_background':phase=='prefill','stale':stale,'phase':phase}
        count,extra=divmod(self.total-active,self.backgrounds)
        begin=active
        for i in range(self.backgrounds):
            n=count+(i<extra);ranges['B'+str(i+1)]=(begin,n);begin+=n
        return {'ranges':ranges,'pause_background':phase=='prefill','stale':stale,'phase':phase}
