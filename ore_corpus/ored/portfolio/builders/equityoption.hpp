#pragma once
#include <ored/portfolio/enginebuilder.hpp>

namespace ore {
namespace data {

class EquityOptionEngineBuilder : public EngineBuilder {
public:
    virtual QuantLib::ext::shared_ptr<QuantLib::PricingEngine>
    engine(const std::string& assetName, const QuantLib::Currency& ccy,
           const QuantLib::Date& expiryDate) = 0;
};

class EquityEuropeanOptionEngineBuilder : public EquityOptionEngineBuilder {
public:
    QuantLib::ext::shared_ptr<QuantLib::PricingEngine>
    engine(const std::string& assetName, const QuantLib::Currency& ccy,
           const QuantLib::Date& expiryDate) override;
};

class EquityAmericanOptionFdEngineBuilder : public EquityOptionEngineBuilder {
public:
    QuantLib::ext::shared_ptr<QuantLib::PricingEngine>
    engine(const std::string& assetName, const QuantLib::Currency& ccy,
           const QuantLib::Date& expiryDate) override;
};

class EquityAmericanOptionBinomialEngineBuilder : public EquityOptionEngineBuilder {
public:
    QuantLib::ext::shared_ptr<QuantLib::PricingEngine>
    engine(const std::string& assetName, const QuantLib::Currency& ccy,
           const QuantLib::Date& expiryDate) override;
};

class EquityAmericanOptionBAWEngineBuilder : public EquityOptionEngineBuilder {
public:
    QuantLib::ext::shared_ptr<QuantLib::PricingEngine>
    engine(const std::string& assetName, const QuantLib::Currency& ccy,
           const QuantLib::Date& expiryDate) override;
};

class EquityAmericanOptionBjerksundEngineBuilder : public EquityOptionEngineBuilder {
public:
    QuantLib::ext::shared_ptr<QuantLib::PricingEngine>
    engine(const std::string& assetName, const QuantLib::Currency& ccy,
           const QuantLib::Date& expiryDate) override;
};

class EquityOptionHestonFdEngineBuilder : public EquityOptionEngineBuilder {
public:
    QuantLib::ext::shared_ptr<QuantLib::PricingEngine>
    engine(const std::string& assetName, const QuantLib::Currency& ccy,
           const QuantLib::Date& expiryDate) override;
};

} // namespace data
} // namespace ore
