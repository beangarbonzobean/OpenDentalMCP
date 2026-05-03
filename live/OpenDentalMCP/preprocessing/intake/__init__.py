"""
Intake auto-filing pipeline for end-of-day batch scans.

This package extends the read-only preprocessing layer with a single module
that WRITES to OD's database: `filer.py`. All other modules in this package
are pure-compute or read-only.

Pipeline:
    watch folder PDF
      -> page-by-page OCR + LLM extraction (extractor.py)
      -> page splitting into document candidates (page_splitter.py)
      -> patient identity matching (patient_matcher.py)
      -> doc category classification (doc_classifier.py)
      -> queue (cache.py) and either auto-file or hold for review
      -> filing (filer.py) writes to OD's document table + image share
      -> audit log row written before every action returns

The processor (processor.py) wires these together.
"""
