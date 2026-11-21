#pragma once
#include <string>

namespace ore {
namespace data {

class EquitySpotQuote {};
class EquityDividendYieldCurve {};
class YieldDiscountCurve {};
class EquityVolatilityCurve {};
class OptionChainSnapshot {};

class Market {
public:
    EquitySpotQuote equitySpot(const std::string& equityName) const;
    EquityDividendYieldCurve equityDividendCurve(const std::string& equityName) const;
    YieldDiscountCurve discountCurve(const std::string& ccy) const;
    EquityVolatilityCurve equityVol(const std::string& equityName) const;
};

} // namespace data
} // namespace ore
