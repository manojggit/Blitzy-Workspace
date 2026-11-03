"""CR00 transaction reports: CORPT00C, the CBTRN03C report builder and JSON/CSV/PDF rendering.

Replaces: CORPT00C (CR00), BMS CORPT00, CBTRN03C (TRANREPT), PROC TRANREPT.

Submodules:
    cbtrn03c: BuildReport and IterateReport, the pure CBTRN03C report line builder.
    report_export: RenderJson, RenderCsv and RenderPdf, the screen JSON and the CSV and PDF
        downloads of those lines.
    corpt00c: RenderTransactionReportJson, RunTransactionReport and ExportTransactionReport,
        the CORPT00C screen flow.

The report is rendered online instead of being submitted through the JOBS TDQ, so it is not
gated by the batch window and never backs up or empties TRANSACT.

This package re-exports nothing. Import each function from its submodule.
"""
