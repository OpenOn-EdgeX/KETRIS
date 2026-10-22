#pragma once
#include "time_bootstrap.hpp"
#include <atomic>
#include <sstream>
#include <string>

// Explicit opt-in for this isolated experiment; production default is unchanged.
#ifndef KETRIS_EXPERIMENT_MAX_CREDIT_CAP_US
#define KETRIS_EXPERIMENT_MAX_CREDIT_CAP_US 1000000
#endif

namespace ketris {
struct RefillCommand { int64_t sequence,grant_us,cap_us; };
inline bool parse_refill(const char* text,RefillCommand& out){
  std::istringstream in(text);RefillCommand value{};std::string extra;
  if(!(in>>value.sequence>>value.grant_us>>value.cap_us) || (in>>extra))return false;
  if(value.sequence<=0 || value.grant_us<0 || value.grant_us>1000000 ||
     value.cap_us<0 || value.cap_us>KETRIS_EXPERIMENT_MAX_CREDIT_CAP_US)return false;
  out=value;return true;
}
// One socket receiver owns sequencing; application threads debit the atomic.
// Experiments bind a fresh process/socket and abort on runtime-generation
// changes. This does not provide general restart fencing. A feeder uses
// increasing host monotonic-ns sequences even after a feeder restart.
struct RefillState {
  int64_t last_sequence=0,accepted=0,rejected=0;
  int64_t active_cap_hits=0;
  int64_t granted_us=0,trimmed_us=0;
  bool apply(std::atomic<int64_t>& credit,const RefillCommand& c){
    if(c.sequence<=last_sequence){++rejected;return false;}
    int64_t old=credit.load(std::memory_order_acquire);
    while(!credit.compare_exchange_weak(old,refill_credit(old,c.grant_us,c.cap_us),
             std::memory_order_acq_rel,std::memory_order_acquire)){}
    if(c.cap_us>0 && old>c.cap_us-c.grant_us)++active_cap_hits;
    granted_us+=c.grant_us;
    // Successful CAS old value makes this exact even with concurrent debits.
    const __int128 trimmed=static_cast<__int128>(old)+c.grant_us-refill_credit(old,c.grant_us,c.cap_us);
    trimmed_us+=static_cast<int64_t>(trimmed);
    last_sequence=c.sequence;++accepted;return true;
  }
};
}
