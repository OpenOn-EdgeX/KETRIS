import {it,expect} from 'vitest';
import {render,screen,cleanup} from '@testing-library/react';
import {DeviceMap} from '../components/DeviceMap';
import type {Snapshot} from '../types';

it('renders assigned PEs separately from observed occupancy and preserves stale/unknown',()=>{
 const snap={mode:'live',devices:[{id:'n:npu0',kind:'npu',name:'RNGD',node:'n',metrics:{pe_count:{value:8}}}],partitions:[{id:'n:npu0',device_id:'n:npu0',kind:'pe',pe_indices:[0,1,2,3,4,5,6,7]}],workloads:[{id:'w',name:'assigned-work',partition_id:'n:npu0',applied:{pe_indices:{value:'4',quality:'observed'}},requested:{},observed:{}}],source_status:{npu:{status:'ok'}}} as unknown as Snapshot;
 render(<DeviceMap snapshot={snap}/>);
 const pe=screen.getByText('PE 4').parentElement!;
 expect(pe.classList.contains('assigned')).toBe(true);expect(pe.classList.contains('occupied')).toBe(false);
 expect(pe.textContent).toContain('배정');expect(pe.textContent).toContain('점유 없음');cleanup();
 snap.source_status.npu.status='stale';render(<DeviceMap snapshot={snap}/>);
 expect(screen.getByText('PE 4').parentElement!.textContent).toContain('점유 미확인');cleanup();
 snap.source_status.npu.status='ok';snap.workloads[0].observed.occupied_pe={value:'4',quality:'stale',source:'npu_driver'};
 render(<DeviceMap snapshot={snap}/>);expect(screen.getByText('PE 4').parentElement!.classList.contains('occupied')).toBe(false);cleanup();
 snap.workloads[0].observed.occupied_pe.quality='observed';render(<DeviceMap snapshot={snap}/>);
 expect(screen.getByText('PE 4').parentElement!.classList.contains('occupied')).toBe(true);cleanup();
});
