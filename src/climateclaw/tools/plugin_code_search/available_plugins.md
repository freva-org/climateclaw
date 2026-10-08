# Freva Plugins

Below is a list of available plugins for decadal prediction skill assessment and related tasks.
Each of the following sections belongs to a specific Freva instance/project, where each entry in it includes

* the plugin name; and
* a brief description of its functionality.

## "coming decade"

* **leadtimeselektor**: extracts and aggregates lead times from decadal prediction ensembles
* **problems**: performs skill score calculation of a decadal climate experiment (and a reference experiment, if applicable) against reanalysis or observation
* **cvprepare**: prepares cross-validation datasets for decadal prediction skill assessment
* **recalibration**: re-calibrates decadal climate data to observations for correction of model drift and bias
* **terciles**: computes tercile-based statistics for prediction skill assessment

---

## "climxtreme"

* **climdexcalc2**: calculates climatological & extreme indices from daily temperature and precipitation data
* **preproc**: pre-processes spatio-temporal data, converting to CMORized formats
* **crops**: assesses the impact of extreme and compound climate events on crop productivity
* **hwmid**: heat wave evaluation using Heat Wave Magnitude Index daily
* **psi**: calculates different precipitation indices for a given precipitation time series
* **precip_return_period_maps**: computes maps of precipitation sums and return periods
* **pca**: performs principal component analysis / Empirical Orthogonal Functions for a given spatio-temporal 3D dataset

---

## "regiklim"

* **Climpact**: processes climate model output data for usage as input of impact model studies.
* **Heat2UrbanImpact**: identifies heatwaves from regional climate model data for usage in urban impact models
* **IDF-CCF**: calculates intensity-duration-frequency, climate change factors
* **Regionmatch**: identifies German districts similar to a region of interest based on climate, sociodemography, orography and land use, supporting regional adaptation planning and knowledge transfer
* **VisualizeClimDexCalc**: visualization of climate indices
* **WayDown**: Temporal disaggregation of daily precipitation to sub-hourly scale with a markov chain model for urban hydrological impact studies

---

## "freva"

* **freva-plugin-template**: defines a template for creating new Freva plugins, including the necessary structure and files for a plugin to be recognized by the Freva system.
