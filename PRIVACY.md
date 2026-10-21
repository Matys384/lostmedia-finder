# Privacy Policy — Lostmedia Finder

Last updated: 21 October 2026

Lostmedia Finder is a free, open-source, non-commercial research tool that runs locally on its user's own computer. It is used to look for public re-uploads or excerpts of historical videos that were deleted by their uploaders (lost-media research).

## What the tool collects

- **No personal data about anyone.** The tool has no user accounts, no sign-in, no analytics, no cookies and no tracking. It does not use Google OAuth and never accesses a YouTube account or private data.
- **Public video metadata only.** Through the YouTube Data API (read-only, API key), Dailymotion's public API and the Internet Archive's public search, it stores public video identifiers, titles, publication dates, durations and channel identifiers.
- **Visual fingerprints.** For videos that are publicly accessible, it computes compact visual fingerprints (image embeddings and perceptual hashes) to compare them with a reference clip. Downloaded media files are temporary and deleted automatically after analysis.

## Where the data is stored

All data stays in a local SQLite database and local files on the computer of the person running the tool. Nothing is uploaded to any server operated by this project; the project operates no server.

## Sharing

Data obtained from YouTube API Services is not sold, shared with third parties, published or redistributed. Results are reviewed by a person and point back to the original public page on the platform (for YouTube, the youtube.com watch page).

## Retention and deletion

- Downloaded media files are deleted right after analysis.
- Stored metadata and fingerprints remain on the local machine until the user deletes the local data folder (`data/`), which removes everything. YouTube API data is refreshed or deleted in line with the YouTube API Services Developer Policies.
- Anyone who believes the tool holds information about them can open an issue on this repository; since the project holds no central database, no data about third parties is retained by the project itself.

## Third-party services

This tool uses YouTube API Services. By using this tool you are also bound by the [YouTube Terms of Service](https://www.youtube.com/t/terms), and Google's handling of data is described in the [Google Privacy Policy](https://policies.google.com/privacy). Google's security settings page for revoking access is <https://security.google.com/settings/security/permissions> (not needed here, as no account access is ever requested).

## Contact

Open an issue on this repository.
