# Workflow

VideoCaptioner separates subtitle content processing from subtitle presentation. Content stages exchange
text, timing, and bilingual structure in memory, with SRT as the only canonical stage format. ASS and
VTT are delivery exports created only when requested.

```text
Media → Transcription → Subtitle optimization/translation → Postprocessing → Synthesis
             │                       │                         │
      Transcription SRT         Initial SRT              Postprocessed SRT
```

Postprocessing is enabled by default in the complete workflow and can be disabled. It works on the
finished initial subtitle after upstream splitting, optimization, and translation. If it fails, the
preserved initial subtitle remains available to downstream stages.

Each completed subtitle stage always writes a canonical SRT checkpoint. The complete GUI workflow can
also export every completed stage as either ASS or VTT using one frozen format, layout, and style
selection. The in-memory handoff remains authoritative; stages do not reread these checkpoint files to
continue processing.

For standalone work, use Subtitle Optimization and Translation first, then import the resulting SRT
into [Subtitle Postprocessing](/en/guide/subtitle-postprocessing). Save the canonical SRT draft or export
a presentation format when the content is ready.
