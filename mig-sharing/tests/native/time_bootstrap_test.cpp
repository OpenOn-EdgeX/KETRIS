#include "time_bootstrap.hpp"
#include <cassert>
#include <limits>

int main(){
  ketris::TimeBootstrap legacy(false), strict(true);
  assert(legacy.mode==0 && legacy.credit==-1 && legacy.unlimited);
  assert(strict.mode==1 && strict.credit==0 && !strict.unlimited);
  assert(ketris::bounded_credit(990000,7500,1000000)==997500);
  assert(ketris::bounded_credit(999999,7500,1000000)==1000000);
  assert(ketris::bounded_credit(-20000,7500,1000000)==-12500);
  // Idle trimming must discard positive banking without forgiving overdraft.
  assert(ketris::refill_credit(50000,0,0)==0);
  assert(ketris::refill_credit(-50000,0,0)==-50000);
  assert(ketris::refill_credit(-50000,10000,0)==-40000);
  assert(ketris::refill_credit(-5000,10000,0)==0);
  assert(ketris::bounded_credit(0,std::numeric_limits<long long>::max(),1000000)==1000000);
  assert(ketris::bounded_credit(0,-7500,1000000)==0);
  assert(ketris::bounded_credit(std::numeric_limits<long long>::min(),7500,1000000)==std::numeric_limits<long long>::min()+7500);
  assert(ketris::wake_threshold(true,true,10000)==0);
  assert(ketris::wake_threshold(false,true,10000)==-10000);
}
