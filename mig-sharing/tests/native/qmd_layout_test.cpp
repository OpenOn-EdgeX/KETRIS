#include "qmd_layout.hpp"
#include <cassert>
#include <string>
int main() {
  // Device has 96 physical coordinates; 7 and 15 are inactive in this fixture.
  std::string input;
  for(int i=0;i<96;i++)if(i!=7&&i!=15){if(!input.empty())input+=",";input+=std::to_string(i);}
  auto map=ketris::parse_tpc_map(input,94);
  assert(map.size()==94);
  auto full=ketris::qmd_disable_mask(map,0,94);
  assert(full[0]==0 && full[1]==0 && full[2]==0 && full[3]==0);
  auto first=ketris::qmd_disable_mask(map,0,24);
  assert(first[0]==0xfc000000u && first[1]==0xffffffffu && first[2]==0xffffffffu && first[3]==0);
  auto last=ketris::qmd_disable_mask(map,24,70);
  assert(last[0]==0x03ff7f7fu && last[1]==0 && last[2]==0 && last[3]==0);
  for(auto bad:{"0,0", "-1,2", "128,2", "1x,2", "1,", ""}) {
    bool rejected=false;try{ketris::parse_tpc_map(bad,2);}catch(...){rejected=true;}assert(rejected);
  }
  bool rejected=false;try{ketris::qmd_disable_mask(map,0,0);}catch(...){rejected=true;}assert(rejected);
}
