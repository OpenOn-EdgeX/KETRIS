#pragma once
#include <cstdint>
#include <limits>

namespace ketris {
struct TimeBootstrap {
  int mode;
  int64_t credit;
  bool unlimited;
  explicit TimeBootstrap(bool start_gated):mode(start_gated?1:0),credit(start_gated?0:-1),unlimited(!start_gated){}
};
// Preserve overdraft; never grant negative credit or overflow the bounded pool.
inline int64_t bounded_credit(int64_t current,int64_t add,int64_t cap){
  if(add<=0)return current;
  if(current>=0 && add>cap-current)return cap;
  if(current>std::numeric_limits<int64_t>::max()-add)return cap;
  int64_t result=current+add;
  return result>cap?cap:result;
}
// Separate from legacy time_add: cap=0 can trim banking without erasing debt.
inline int64_t refill_credit(int64_t current,int64_t add,int64_t cap){
  int64_t result=bounded_credit(current,add,cap);
  return result>cap?cap:result;
}
inline int64_t wake_threshold(bool strict,bool urgent,int64_t borrow){
  return !strict && urgent ? -borrow : 0;
}
}
