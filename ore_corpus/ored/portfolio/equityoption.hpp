#pragma once
#include <ored/portfolio/optiondata.hpp>
#include <ored/portfolio/trade.hpp>

namespace ore {
namespace data {

//! ORE EquityOption Trade
class EquityOption : public Trade {
public:
    EquityOption(const Envelope& env,
                 const OptionData& option,
                 const std::string& equityName,
                 const std::string& currency,
                 double strike,
                 double quantity);

    void build(const boost::shared_ptr<EngineFactory>& engineFactory) override;

    const OptionData& option() const { return option_; }
    const std::string& equityName() const { return equityName_; }
    const std::string& currency() const { return currency_; }
    double strike() const { return strike_; }
    double quantity() const { return quantity_; }

private:
    OptionData option_;
    std::string equityName_;
    std::string currency_;
    double strike_;
    double quantity_;
};

} // namespace data
} // namespace ore
