"""Processing jobs of the CardDemo batch chain, one module per legacy processing job.

Modules, with the JCL job and COBOL programs each replaces and its step in the chain:
    tranbkp: TRANBKP, steps 6 and 15.
    posttran: POSTTRAN, program CBTRN02C, step 13.
    interest_calc: INTCALC, program CBACT04C, step 14.
    combtran: COMBTRAN, step 16.
    statement_gen: CREASTMT, programs CBSTM03A and CBSTM03B, step 17. It is a subpackage whose
        job module is statement_gen.creastmt.
    tranidx: TRANIDX, step 18.

Each job module exposes JOB_NAME, JCL_NAME and RunJob(context: JobContext) -> JobResult.
The chain in batch.orchestration.batch_chain imports each job module by its path only when the
step runs, so this package imports nothing and re-exports nothing. docs/batch.md documents what
each job reads, writes and returns.
"""
