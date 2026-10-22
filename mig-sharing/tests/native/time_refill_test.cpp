#include "time_refill.hpp"
#include <cassert>
#include <atomic>
#include <thread>
#include <string>

int main(){
  ketris::RefillCommand c{};
  assert(ketris::parse_refill("1 10000 250000",c));
  assert(c.sequence==1 && c.grant_us==10000 && c.cap_us==250000);
  for(const char* bad:{"", "1 2", "0 2 3", "-1 2 3", "1 -2 3", "1 2 -3",
      "1 1000001 3", "1 2 1000001", "1 2 3 extra", "9223372036854775808 2 3"})
    assert(!ketris::parse_refill(bad,c));
  ketris::RefillState state;std::atomic<int64_t> credit{-50000};
  assert(state.apply(credit,{1,10000,250000}));assert(credit==-40000);
  assert(!state.apply(credit,{1,10000,250000}));assert(credit==-40000);
  assert(!state.apply(credit,{0,10000,250000}));assert(credit==-40000);
  assert(state.apply(credit,{2,0,0}));assert(credit==-40000);
  credit=50000;assert(state.apply(credit,{3,0,0}));assert(credit==0);
  assert(state.last_sequence==3 && state.accepted==3 && state.rejected==2);
  credit=249999;assert(state.apply(credit,{4,10000,250000}));
  assert(credit==250000 && state.active_cap_hits==1);
  // The runtime receiver is the single refill producer; debits can race it.
  credit=0;ketris::RefillState concurrent;
  std::thread charger([&]{for(int i=0;i<100000;i++)credit.fetch_sub(1);});
  for(int i=1;i<=100000;i++)assert(concurrent.apply(credit,{i,0,0}));
  charger.join();assert(credit==-100000);
  credit=0;ketris::RefillState sums;
  std::thread charges([&]{for(int i=0;i<100000;i++)credit.fetch_sub(1);});
  for(int i=1;i<=100000;i++)assert(sums.apply(credit,{i,1,1000000}));
  charges.join();assert(credit==0);
}
