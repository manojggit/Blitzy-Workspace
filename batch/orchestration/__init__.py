"""Replaces: scheduler/CardDemo.ca7, scheduler/CardDemo.controlm (CA-7 and Control-M chains),
JCL condition codes, GDG bases, CLOSEFIL/OPENFIL.

Submodules: job_context (exit statuses, JobParameters, JobContext, JobResult, JobFailure),
clock (Clock, ParseAsOf), batch_window (BatchWindow, CLOSEFIL/OPENFIL), run_ledger (cycles,
step identity, run history), gdg (generation archives and catalog) and batch_chain (the 18-step
chain and command entry points). This package re-exports nothing and has no import side effects.
"""
