# free-stealth-bench automated judge ranking

Rubric `v1` · judge `opencode/nemotron-3.5-super-vl-free` · input mode `image` · updated 2026-11-25T07:19:25+00:00

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
 11  tidepool-alpha-free                     73.50   3.5   3.0   3.5   3.5   4.0   3.5   5.0    0
 12  flint-40b-free                          73.30   3.5   3.5   3.5   3.0   3.5   3.5   5.0    0
 13  nemotron-3.5-lightning-free             63.50   2.5   3.5   3.0   2.5   3.0   2.5   5.0    0
 14  mimo-v2.6-flash-free                    45.70   2.0   2.5   2.0   2.5   2.5   2.0   2.5    1
 15  longcat-2.5-free                        44.50   2.0   2.5   2.0   2.0   2.5   2.0   2.5    1
 16  particle-1:free@openai                  44.50   2.0   2.5   2.0   2.0   2.5   2.0   2.5    1
 17  pulse-alpha-free                        43.50   2.0   2.5   2.0   2.0   2.0   2.0   2.5    1
 18  dots-3-note-preview:free@openai         41.10   2.0   2.5   2.0   1.0   2.0   2.0   2.5    1
 19  nemotron-3-nano-omni-30b-a3b-reasoning:free@openai  29.60   1.0   1.5   1.5   1.0   1.5   1.5   2.5    1
 20  crouton-alpha-free                       0.00   0.0   0.0   0.0   0.0   0.0   0.0   0.0    2
 21  ernie-5.5-flash-preview:free@openai      0.00   0.0   0.0   0.0   0.0   0.0   0.0   0.0    2
 22  gemma-4-26b-a4b-it:free@openai           0.00   0.0   0.0   0.0   0.0   0.0   0.0   0.0    2
 23  gemma-4-31b-it:free@openai               0.00   0.0   0.0   0.0   0.0   0.0   0.0   0.0    2
 24  ling-3.2-flash:free@openai               0.00   0.0   0.0   0.0   0.0   0.0   0.0   0.0    2
 25  lfm-3-1.7b:free@openai                   0.00   0.0   0.0   0.0   0.0   0.0   0.0   0.0    2
 26  nemotron-3-ultra-550b-a55b:free@openai   0.00   0.0   0.0   0.0   0.0   0.0   0.0   0.0    1
 27  nemotron-3-ultra-550b-a55b:free          0.00   0.0   0.0   0.0   0.0   0.0   0.0   0.0    1
 28  nemotron-3.5-lightning:free@openai       0.00   0.0   0.0   0.0   0.0   0.0   0.0   0.0    1
 29  nemotron-3.5-lightning:free              0.00   0.0   0.0   0.0   0.0   0.0   0.0   0.0    1
 30  nemotron-3.5-super-vl:free@openai        0.00   0.0   0.0   0.0   0.0   0.0   0.0   0.0    1
 31  nemotron-3.5-super-vl:free               0.00   0.0   0.0   0.0   0.0   0.0   0.0   0.0    1
 32  flint-v1.0:free@openai                   0.00   0.0   0.0   0.0   0.0   0.0   0.0   0.0    2
```

Dimensions: output_quality, instruction_following, creativity, animation_fidelity, richness_detail, composition_aesthetics (1-5, judged blind) and technical_validity (1-5, deterministic). DNF runs score 0. See `judgments.json` for per-run reasons.
