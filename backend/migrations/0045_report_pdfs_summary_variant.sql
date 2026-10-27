-- 0045: the two-page summary report is a stored report type.
--
-- 0043 allowed three report types in report_pdfs.variant ('free', 'full', 'long'). The two-page summary report (variant 'summary', the default download of the Consent Agent
-- console) was added later; without this the pre-generated copy could not be stored (the check constraint refused it, the service logged "Could not keep the PDF report" and every
-- download printed the report again). Relaxes one check constraint; no row, column or other constraint is touched, and re-running it changes nothing.
alter table report_pdfs drop constraint if exists report_pdfs_variant_check;
alter table report_pdfs add constraint report_pdfs_variant_check check (variant in ('free', 'full', 'long', 'summary'));
