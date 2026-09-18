# V0 E2E Reference Project

The reference project is intentionally synthetic in its execution results but must use real research sources.

Example user intent:

> 设计一个用于低浓度 CO₂ 捕获的材料研究项目。希望在潮湿条件下仍有较好性能，同时考虑成本和可实验验证性。

Rules:
- Research Agent must perform live real research.
- Research may identify MOF/COF/amine/material families based on real evidence.
- The benchmark is not fixed to one material family in advance.
- Compute outputs are simulated by MockComputeBackend.
- Lab outputs are simulated by MockLabBackend.
- Simulated outputs must never be registered as external scientific evidence.
- They can be project execution artifacts used to test workflow logic.
- At least one failure and one lab deviation must be injected.
- Master must replan.
