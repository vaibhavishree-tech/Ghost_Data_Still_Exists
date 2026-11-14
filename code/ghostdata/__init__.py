"""ghostdata: the deletion-audit pipeline.

The package is layered; each module only depends on the ones listed before it.

  config -> io_utils -> models              constants, small helpers and shared data classes
  extractors -> normalize -> parsers -> store
                                            read every evidence file (SQL, JSON, logs, text, PDF,
                                            screenshots) into one table of NormRecords  (Phase 1)
  validate                                  reconcile that table with the raw files
  identity -> policy -> steps               who a record belongs to, what the deletion rules
                                            require, how the answer steps are ordered
  linker -> pipeline                        activity-log linking and the per-request audit engine
                                            that produces the final status and steps  (Phase 2)
"""