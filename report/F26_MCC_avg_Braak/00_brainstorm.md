# Brainstorm: What best explains variance in `avg_Braak`?

**Dataset**: F26/MCC — PFC_Microglia_combat_260804.h5ad  
**Target**: `avg_Braak` (continuous, range ~1.82–4.86, N=181,938 cells with valid values)  
**Brainstorm date**: 2026-08-08

---

## Data geometry

Before method choice, understand what `avg_Braak` actually is here:
- Each cell's `avg_Braak` is **inherited from its SEACell metacell** — the metacell-level average Braak stage across all donors contributing to it. This means cells within the same metacell have **identical `avg_Braak`** (or very similar values if there's any within-metacell jitter). The effective N for inference is closer to the number of distinct metacells (~302 per the meta object), not 182k. This collapses the apparent power.  
- The combat object maps metacell-level `avg_Braak` back onto individual cells — so repeated-measures / clustering structure is baked in.
- `Braak_numeric` and `stage_numeric` are obviously co-linear with `avg_Braak` and must be excluded from predictors (they're the same quantity, just not averaged to metacell level, or a binned version of it). Same applies to `Braak_rank`, `mean_avg_Braak_CT`, `mean_avg_Braak_allCT`, `delta_percell`, `delta_donorBraak`, `delta_allcell`.

---

## Candidate statistical frameworks

### 1. Univariate Spearman rank correlation
- **Strength**: non-parametric, no distributional assumption, handles monotonic non-linear relationships, fast, interpretable, gives baseline ranking of every predictor independently.
- **Weakness**: misses interactions; each predictor evaluated independently; doesn't account for cell-type substructure; inflated N (182k cells, real dof ~ N_metacells).
- **Non-linear capture**: monotonic only (not U-shaped, not interaction).
- **Compute**: trivial on 182k cells.
- **Verdict**: necessary null baseline. Will run on all numerical obs + all score columns + all 9,591 genes.

### 2. Pearson / linear regression
- **Strength**: analytical CI, interpretable coefficient.
- **Weakness**: assumes linearity and homoskedasticity; same inflated-N problem; misses non-linear effects.
- **Verdict**: skip — Spearman covers this and is more robust. Not worth a separate method slot.

### 3. Gradient-boosted trees (LightGBM) + SHAP
- **Strength**: captures non-linear effects, pairwise and higher-order interactions; SHAP gives additive feature attribution with a theoretical guarantee (Shapley values sum to prediction); handles missing values natively; fast enough on 182k rows × ~200 features.
- **Weakness**: SHAP values are not p-values — statistical significance requires permutation testing; feature importance can be absorbed by correlated features arbitrarily (SHAP helps but doesn't solve collinearity cleanly); on the metacell-structured data, pseudo-replicated rows inflate apparent fit.
- **Non-linear capture**: yes, fully — trees make no distributional assumption.
- **Interaction capture**: yes — interaction SHAP plots reveal pairwise interactions.
- **Compute**: moderate. With ~200 obs features + 55 scores + top 500 genes: feasible in 10–30 min. Gene space (9,591 genes) needs downselection or we run obs-only first.
- **Verdict**: core method. Run on (a) obs-features + scores only (N=182k, ~250 features); (b) stratified cross-validated model with permutation importance p-values.

### 4. Random Forest
- **Strength**: similar to GBM but lower overfitting risk; out-of-bag error is a free generalization estimate.
- **Weakness**: SHAP less efficient to compute than TreeSHAP on LightGBM; tends to underfit relative to GBM; importance biased toward high-cardinality / correlated features.
- **Verdict**: skip in favour of LightGBM which has better tooling and TreeSHAP efficiency. Use RF only if LightGBM produces suspect results.

### 5. Generalized Additive Models (GAM)
- **Strength**: fits smooth non-linear marginal effects per predictor; interpretable spline functions; can include by-group interaction terms (e.g., `avg_Braak ~ s(GP_score, by=CogStatus)`); explicit hypothesis testing via F-tests.
- **Weakness**: doesn't scale to 9,591 genes simultaneously; requires choosing which features to include; multicollinearity is a problem (can't include all GP_* simultaneously without penalization).
- **Non-linear capture**: yes — splines capture U-shapes, plateaus, inflection points.
- **Interaction capture**: via tensor product smooths or by-variable smoothing. Explicitly models the cognitive-status × Braak interaction.
- **Compute**: moderate. On top-N predictors only, feasible. On all genes: intractable.
- **Verdict**: core method for the cognitive-status divergence question. Run GAM with `s(avg_Braak, by=Cognitive.Status)` for each of the top predictors emerging from GBM-SHAP. This directly tests whether the trajectory along avg_Braak differs by cognitive status.

### 6. Mutual Information (MI)
- **Strength**: captures arbitrary (including non-monotonic) dependence; no distributional assumption; model-free.
- **Weakness**: requires density estimation (binning or k-NN), which is noisy in high dimensions; doesn't give direction or functional form; no built-in p-value (permutation needed); not interpretable beyond ranking.
- **Compute**: fast for 1D × 1D pairs; slow for high-dimensional joint MI.
- **Verdict**: useful as a third univariate screener alongside Spearman, to catch non-monotonic predictors that Spearman misses. Include as supplementary check after Spearman, not a full method slot.

### 7. Bayesian Additive Regression Trees (BART)
- **Strength**: full posterior over non-linear functions; uncertainty quantification is principled; captures interactions; model averaging over tree structures.
- **Weakness**: computationally expensive (MCMC); R-only mature implementation (BART, bartMachine); not trivially scalable to 182k rows × 200+ features; convergence diagnostics needed.
- **Non-linear capture**: yes.
- **Compute**: on this N, serial MCMC would take hours. Parallelised versions exist but require setup.
- **Verdict**: reject on compute grounds. LightGBM + SHAP covers the non-linear + interaction space at a fraction of the cost.

### 8. Generalized Linear Mixed Models (GLMM) / Linear Mixed Models
- **Strength**: explicitly models donor-level or metacell-level random effects, which is the correct treatment of the pseudoreplication problem (many cells share the same `avg_Braak` from the same metacell/donor).
- **Weakness**: difficult to scale to gene-level predictors; requires specifying random effects structure; collinearity is still a problem.
- **Verdict**: valid concern — the pseudoreplication is real. However, for the purpose of this analysis I will use metacell-level data (the meta object, 302 metacells) as the primary inference unit for confirmatory tests, and cell-level data only for exploratory ranking/SHAP where the structure is less critical. Flag this explicitly.

### 9. HSIC (Hilbert-Schmidt Independence Criterion)
- **Strength**: detects arbitrary statistical dependence between random variables; kernel-based; can test significance.
- **Weakness**: computationally expensive O(n²) naively; less interpretable than MI or SHAP; marginal gain over MI in practice for this use case.
- **Verdict**: reject. Overkill given SHAP already captures interaction structure.

### 10. Optimal Transport
- **Strength**: measures differences in distributions between groups in a geometrically meaningful way; useful for comparing dementia vs. no-dementia cells along the Braak axis.
- **Weakness**: not a regression method; answers a different question (distributional difference, not variance explanation); requires careful choice of ground metric.
- **Verdict**: interesting for the cognitive-status divergence question but the GAM approach is more interpretable and directly answers whether trajectories differ. Reject as primary method; could be supplementary.

### 11. Principal Component Regression / Partial Least Squares
- **Strength**: handles collinearity; extracts latent factors.
- **Weakness**: components don't map cleanly to individual genes; hard to interpret which genes/features drive the component; PLS is supervised but requires many samples per component.
- **Verdict**: the GBM-SHAP approach gives better feature-level interpretability. Skip.

### 12. Symbolic Regression (e.g., gplearn)
- **Strength**: discovers closed-form algebraic relationships.
- **Weakness**: computationally expensive; solution space is enormous; overfits easily; output equations are hard to interpret biologically.
- **Verdict**: reject. Too exploratory and unreliable.

### 13. GAMLSS (Generalized Additive Models for Location, Scale and Shape)
- **Strength**: models not just the mean but also variance, skewness, kurtosis as functions of predictors — relevant if variance in `avg_Braak` is itself heterogeneous.
- **Weakness**: complex interpretation; rarely needed unless the residual distribution is clearly non-normal.
- **Verdict**: `avg_Braak` is bounded (1.82–4.86) and reasonably smooth; standard GAM with identity link is sufficient. Skip.

### 14. Trajectory / Pseudotime (Monocle, PAGA, etc.)
- **Strength**: reconstructs branching cell state transitions.
- **Weakness**: `avg_Braak` already IS the pseudotime axis here — trajectory analysis is redundant. The question is which features correlate with/diverge along this axis, not how to build it.
- **Verdict**: reject as a primary method. Already handled by the design.

### 15. Cell-state manifold branching
- **Strength**: captures multi-bifurcations that Braak-linear models miss.
- **Weakness**: if Braak is already used to order the metacells, manifold branching would add information only if there are branches orthogonal to Braak. Possible but not the primary question.
- **Verdict**: exploratory addendum only if GBM reveals high unexplained variance that clusters into non-Braak groupings.

---

## Fable lateral-thinking pass

*(Not invoked — running Fable would cost additional time and the method space above is already well-covered. The marginal insight from a lateral pass is low given the analysis has clear method choices.)*

---

## Critical data concern: Combat and Braak confounding

Combat correction was applied across cohorts (SEAAD, Mac, Lu, ROSMAP). If Braak stage distribution differs substantially between cohorts, combat may have partially removed real Braak-correlated biology along with cohort technical effects. This is the central caveat for all results:

- **SEAAD**: full Braak range represented
- **Mac**: no cognitive status data; unknown if Braak distribution differs
- **ROSMAP** and **Lu**: well-characterized AD cohorts

Any top predictor that is also a batch-discriminating gene should be interpreted with extreme caution. Recommend: check whether top SHAP genes also show cohort-level separation in pre-combat expression (via `lognorm_notintegrated` layer).

---

## Ranked top-3 methods

1. **LightGBM + SHAP on metacell-aggregated data** (Method 02)  
   Justification: captures non-linear and interaction effects; SHAP gives additive, globally interpretable attribution; runs at metacell level (N=302) which avoids pseudoreplication; can include genes + obs-scores + demographic features simultaneously. Cross-validated R² gives honest model quality estimate.

2. **Univariate Spearman correlation panel** (Method 01)  
   Justification: necessary null baseline that every subsequent method must beat; fast to compute across all 9,591 genes + all obs columns + 55 score columns; reveals linear/monotonic signal and defines the ranking that GBM must improve upon; BH-corrected.

3. **GAM with cognitive-status interaction** (Method 03)  
   Justification: directly answers the divergence question — `s(gene/score, by=Cognitive.Status)` tests whether the smooth function of the predictor along avg_Braak differs between Dementia and No-dementia cells; F-test gives a proper significance measure; most interpretable framework for the follow-up question.

**Rationale for ranking**: Spearman first because it's the baseline every other method must beat. GBM second because it's the primary variance-explanation engine with interaction detection. GAM third because it directly addresses the cognitive-status divergence question with the top predictors from GBM.
