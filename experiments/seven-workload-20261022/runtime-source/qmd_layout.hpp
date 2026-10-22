#pragma once
#include <array>
#include <string>
#include <vector>
#include <stdexcept>
#include <charconv>
namespace ketris {
inline std::vector<unsigned> parse_tpc_map(const std::string& text, unsigned expected) {
  std::vector<unsigned> out; std::array<bool,128> seen{};
  size_t pos=0;
  while(pos<text.size()) {
    auto end=text.find(',',pos);if(end==std::string::npos)end=text.size();
    unsigned n=0;auto parsed=std::from_chars(text.data()+pos,text.data()+end,n);
    if(parsed.ec!=std::errc{} || parsed.ptr!=text.data()+end || n>=128 || seen[n])
      throw std::invalid_argument("invalid calibrated TPC map");
    seen[n]=true;out.push_back(n);pos=end+1;
  }
  if(text.empty() || text.back()==',' || out.size()!=expected)
    throw std::invalid_argument("TPC map count mismatch");
  return out;
}
inline std::array<unsigned,4> qmd_disable_mask(const std::vector<unsigned>& map, unsigned begin, unsigned count) {
  if(map.empty() || begin>=map.size() || count==0 || count>map.size())
    throw std::invalid_argument("invalid TPC range");
  std::array<unsigned,4> mask{};
  // Leave all uncalibrated coordinates untouched; only restrict known live ones.
  for(unsigned i=count;i<map.size();i++) {
    auto bit=map[(begin+i)%map.size()];mask[bit/32]|=1u<<(bit%32);
  }
  return mask;
}
}
