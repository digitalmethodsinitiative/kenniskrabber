# 🤔Kenniskrabber
<p align="center">
  <img width="357" height="286" alt="afbeelding" style="border: 2px solid black;" src="https://github.com/user-attachments/assets/c76ac3b8-da0d-4027-8aa9-9b23a84c857a" />
</p>
Kenniskrabber is a semi-automated tool to collect and analyze data from Google AI Overviews and AI Mode answers.

While it does not circumvent any CAPTCHAs, Kenniskrabber does speed up the [means to observe](https://policyreview.info/articles/analysis/towards-platform-observability) AI responses in search. 

## What can I collect?
1. AI Mode and AI Overview responses as a CSV
2. Full-page screenshots
3. HTML of the Google SERP and AI Mode answers
4. HTML of the linked-to sources
5. (coming soon) Metadata and analyses

## Installation
You can run Kenniskrabber as a NiceGUI app through Python:

```
python main.py
```

Or run it as an installable app:

### Mac OS
Install the `.dmg` file and indicate you trust the app.

### Windows
Install with the `.exe` file and indicate you trust the app (`More info` -> `Run Anyway`).

## Help, things broke
That happens! You can edit the relevant CSS selectors in the Kenniskrabber interface yourself, but please also submit a GitHub issue to notify the developers.

## Credits & license
Kenniskrabber is created by [Sal Hagen](https://salhagen.nl) as part of the [Deep Culture](https://deep-culture.org) project and [Digital Methods Initiative](https://digitalmethods.net).
