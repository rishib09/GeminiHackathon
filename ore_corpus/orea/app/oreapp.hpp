#pragma once
#include <orea/app/inputparameters.hpp>

namespace ore {
namespace analytics {

class NpvAnalytic {};
class CashflowAnalytic {};
class SensitivityAnalytic {};

class OREApp {
public:
    explicit OREApp(const InputParameters& inputs);
    int run();
    NpvAnalytic npvReport() const;
    CashflowAnalytic cashflowReport() const;
};

} // namespace analytics
} // namespace ore
