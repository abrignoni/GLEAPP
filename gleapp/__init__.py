"""GLEAPP - Graphics · Logs · Examination · Automated Processing · Parsing.

An open-source media forensics toolkit for triaging and analyzing large sets of
images and video in a digital-forensics workflow.

Core capabilities
-----------------
* Recursive ingestion of images/video from folders or mounted evidence
* Cryptographic hashing (MD5/SHA1/SHA256) + perceptual hashing (aHash/pHash/dHash)
* Exact-duplicate *stacking* and near-duplicate clustering
* Known-hash list import/matching (Project VIC / CAID / plain CSV)
* EXIF / metadata extraction, GPS geolocation, capture timelines
* Thumbnail generation and video key-frame extraction
* Face detection and simple skin-tone screening (OpenCV)
* Manual categorization using an examiner-defined category scheme (named per case)
* Review tracking ("already seen") to cut redundant viewing
* HTML / CSV / JSON reporting and KML map export
* A local web review gallery (Flask)
"""

__version__ = "0.1.0"
