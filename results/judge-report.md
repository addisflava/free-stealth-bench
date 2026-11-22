# free-stealth-bench automated judge ranking

Rubric `v1` · judge `opencode/inkling-2-small-free` · input mode `image` · updated 2026-11-22T19:26:52+00:00

Weights: output_quality=0.22, instruction_following=0.2, creativity=0.1, animation_fidelity=0.12, richness_detail=0.1, composition_aesthetics=0.11, technical_validity=0.15

```
  #  model                                   score  qual instr  crea  anim  rich aesth  tech  dnf
-------------------------------------------------------------------------------------------------
  1  deepseek-v4.1-flash-free                94.50   4.5   5.0   4.5   4.5   5.0   4.5   5.0    0
  2  glm-5.3-flash-free                      92.50   4.0   5.0   4.0   5.0   5.0   4.5   5.0    0
  3  big-pickle                              91.40   4.0   5.0   4.0   5.0   5.0   4.0   5.0    0
  4  inkling-2-small-free                    90.20   4.0   5.0   4.0   4.5   5.0   4.0   5.0    0
  5  crouton-alpha@openai                    89.00   4.0   5.0   4.0   4.0   5.0   4.0   5.0    0
  6  nemotron-3.5-super-vl-free              86.20   4.0   4.5   4.0   4.5   4.0   4.0   5.0    0
  7  hy4-flash-free                          86.00   4.0   4.5   4.0   4.0   4.5   4.0   5.0    0
  8  beam-free                               85.30   4.0   4.0   3.5   4.5   4.5   4.5   5.0    0
  9  minimax-m3.1-flash-free                 83.80   4.0   4.5   3.5   3.5   4.5   4.0   5.0    0
 10  iceman-free                             77.70   3.5   3.5   4.0   4.0   4.0   3.5   5.0    0
 11  flint-40b-free                          73.30   3.5   3.5   3.5   3.0   3.5   3.5   5.0    0
 12  nemotron-3.5-lightning-free             63.50   2.5   3.5   3.0   2.5   3.0   2.5   5.0    0
 13  mimo-v2.6-flash-free                    45.70   2.0   2.5   2.0   2.5   2.5   2.0   2.5    1
 14  longcat-2.5-free                        44.50   2.0   2.5   2.0   2.0   2.5   2.0   2.5    1
 15  pulse-alpha-free                        43.50   2.0   2.5   2.0   2.0   2.0   2.0   2.5    1
 16  crouton-alpha-free                       0.00   0.0   0.0   0.0   0.0   0.0   0.0   0.0    2
 17  tidepool-alpha-free                      0.00   0.0   0.0   0.0   0.0   0.0   0.0   0.0    2
```

Dimensions: output_quality, instruction_following, creativity, animation_fidelity, richness_detail, composition_aesthetics (1-5, judged blind) and technical_validity (1-5, deterministic). DNF runs score 0. See `judgments.json` for per-run reasons.
