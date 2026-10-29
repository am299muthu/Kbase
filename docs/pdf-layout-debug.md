# PDF object geometry debug view

In Ingestion monitor, open Details for a PDF, then **PDF bounding boxes**.
Select a page to see numbered rectangles on a blank page outline and exact
left/top/right/bottom coordinates, page dimensions, object IDs, types and extractors.
Blue boxes are Docling layout detections; orange boxes are processing crops.
These may overlap: a layout table and its processing crop are separate records.
Download geometry JSON for offline analysis. No text, images or credentials are included.

Makers, checkers, owners and delegates for the document folder can access
`GET /api/processing-requests/{request_id}/pdf-layout`.
New runs save geometry immediately after successful parsing, before vision or
embedding calls, in an `ingestion.pdf_layout` audit event. Parser failures before
completion have no snapshot. Detection boxes represent parser-recognized regions,
not every low-level PDF drawing primitive. Coordinates have a top-left origin,
use the extractor's page units (usually PDF points), and are not rendered pixels.

Older runs use their stored parsed artifact, exposing visual crop boxes only.
A new run is needed to capture their full Docling text/layout regions. The viewer
uses blank page outlines and does not expose original unredacted PDF content.
