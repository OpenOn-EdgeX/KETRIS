import {describe,it,expect} from 'vitest';
import {formatValue,valueLabel,workloadColor,windowLabel,markStale,stateLabel,gpuConnectionsKnown} from '../presentation';
describe('truthful observation presentation',()=>{
 it('requires explicit fresh empty runtime provenance rather than legacy cached absence',()=>{
  const s:any={mode:'live',source_status:{gpu:{status:'ok'},kubernetes:{status:'ok'},checkpoint:{status:'ok'},runtime:{status:'unavailable',reason:'no active MIG runtime export'}}};
  expect(gpuConnectionsKnown(s)).toBe(false);
  s.source_status.runtime.reason='no active MIG runtime export (fresh lookup)';
  expect(gpuConnectionsKnown(s)).toBe(true);
  s.source_status.runtime.status='stale';
  expect(gpuConnectionsKnown(s)).toBe(false);
 });
 it('separates observed settings and missing/zero',()=>{
  expect(valueLabel('configured')).toBe('설정');expect(valueLabel('observed')).toBe('실측');
  expect(formatValue({value:null,unit:'%',quality:'unavailable',source:'runtime'})).toBe('—');
  expect(formatValue({value:0,unit:'%',quality:'observed',source:'runtime'})).toBe('0%');
  expect(valueLabel('stale')).toBe('오래된 값');
 });
 it('keeps workload labels and sparse intervals stable',()=>{
  expect(workloadColor({id:'archive:space:A',name:'A'})).toBe(workloadColor({id:'archive:combined:A',name:'A'}));
  expect(windowLabel({condition:'combined',repeat:2,start:20,end:40.052})).toContain('20.0–40.1s');
  expect(workloadColor({id:'node:uid:work',name:'waiting'})).toBe(workloadColor({id:'node:uid:work:a-123',name:'waiting'}));
 });
 it('marks last successful live values stale without replacing unknown by zero',()=>{
  const value:any={schema_version:1,node:'n',mode:'live',captured_at:'2026-10-13T00:00:00Z',devices:[{metrics:{util:{value:0,quality:'observed'},missing:{value:null,quality:'unavailable'}}}],partitions:[],workloads:[],source_status:{gpu:{status:'ok'}}};
  const stale=markStale(value);expect(stale.devices[0].metrics.util.quality).toBe('stale');expect(stale.devices[0].metrics.util.value).toBe(0);
  expect(stale.devices[0].metrics.missing.value).toBe(null);expect(value.devices[0].metrics.util.quality).toBe('observed');
  expect(stateLabel('Pending')).toBe('대기 중');
 });
});
