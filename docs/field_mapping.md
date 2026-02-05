# Field mapping (eniq.ai → DB `analyses`)

| Column | Type | Source | Description |
| --- | --- | --- | --- |
| `id` | int (PK) | `data.id` | Analysis ID from API |
| `uuid` | str | `data.uuid` | Analysis UUID (if provided) |
| `task_id` | str | `data.task_id` | Task ID as string |
| `task_uuid` | str | `data.task_uuid` | Task UUID |
| `project_id` | int | `data.project_id` | Project ID |
| `status` | str | `data.status` | Analysis status (e.g. `completed`) |
| `content_type` | str | `data.content_type` | Content type from API |
| `model` | str | `data.model` | Model name |
| `processing_time_ms` | int | `data.processing_time_ms` | Processing time in ms |
| `department` | str | `data.department` | Department label |
| `representative` | str | `data.representative` | Agent/manager name |
| `prompt_id` | int | `data.prompt.id` | Prompt ID |
| `prompt_name` | str | `data.prompt.scriptName` | Prompt/script name |
| `call_date` | datetime | `data.call_date` | Call date/time |
| `communication_date` | datetime | `data.communication_date` | Communication date/time |
| `created_at` | datetime | `data.created_at` | Analysis created timestamp |
| `updated_at` | datetime | `data.updated_at` | Analysis updated timestamp |
| `duration_minutes` | float | `data.duration` | Duration in minutes (float) |
| `external_url` | text | `data.external_url` | Link to audio |
| `metadata_json` | JSON | `data.metadata` | Raw metadata (e.g. Customer, Department, Representative, ExternalUrl, uniqueid, error fields) |
| `result_json` | JSON | `data.result` | Raw scoring/result fields (`Final Score`, `Загальна оцінка`, `Результат дзвінка`, `Summary`, `Title`, flags/booleans, comments, etc.) |
| `audio_file_json` | JSON | `detail.audio_file` | Audio file info (play/download URLs, duration, format, file ids, status) |
| `transcription_text` | text | `detail.transcription.text` | Full transcript text |
| `transcription_json` | JSON | `detail.transcription` | Full transcription payload (language, duration, status, segments with start/end/channel/text) |
| `tokens_json` | JSON | `detail.tokens_used` + `detail.tokens_used_json` | Token usage (totals + breakdown) |
| `summary` | text | `data.result.Summary` | Cached summary for quick access |
| `title` | text | `data.result.Title` | Cached title for quick access |

Notes:
- `detail` refers to payload from `GET /analysis/{id}` (always fetched during ingest).
- Timestamps are stored as datetimes parsed from ISO strings returned by the API (UTC when `Z` suffix is present).
- For analytics, prefer JSON extraction for specific result fields (e.g., `result_json ->> 'Final Score'`). Index `call_date` is added for faster time-based queries.***
