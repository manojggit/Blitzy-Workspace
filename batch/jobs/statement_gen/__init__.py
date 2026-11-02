"""Statement generation: job CREASTMT (jcl/CREASTMT.JCL), step 17 of the chain, files only.

Replaces: CREASTMT, programs CBSTM03A and CBSTM03B.

Parts:
    creastmt: the CREASTMT job step (RunJob). One ordered query replaces the STEP010 sort and
        OUTREC re-key, and it writes STATEMNT.PS (80-column lines) and STATEMNT.HTML
        (100-column lines) under CARDDEMO_OUTPUT_DIR/statements/.
    cbstm03a: the CBSTM03A statement builder, pure and without I/O.
    cbstm03b: the CBSTM03B data access, as synchronous SQLAlchemy Core queries.
    templates/statement.html.j2: the CBSTM03A HTML-L01 to HTML-L80 literals with field slots.

The chain in batch.orchestration.batch_chain imports statement_gen.creastmt by its path only
when the step runs, so this package imports nothing and re-exports nothing.
"""
