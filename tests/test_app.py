from dataclasses import asdict

import app as app_module
from app import SourceTrack, _bulk_transfer_worker, _clean_source_title, _split_artist_title, _youtube_url_ok, normalize_artist, normalize_text, score_candidate


def test_normalize_text_removes_video_noise():
    assert normalize_text("夜に駆ける（Official Music Video）") == "夜に駆ける"
    assert normalize_text("Song [Remastered 2024]") == "song"


def test_normalize_artist_removes_topic_suffix():
    assert normalize_artist("宇多田ヒカル - Topic") == "宇多田ヒカル"


def test_split_artist_title_when_channel_matches():
    title, artist = _split_artist_title("Adele - Hello", "AdeleVEVO")
    assert title == "Hello"
    assert artist == "Adele"


def test_clean_source_title_removes_channel_suffix():
    assert _clean_source_title(
        "Adventures - A Himitsu | Royalty Free Music - No Copyright Music",
        "Royalty Free Music - No Copyright Music",
    ) == "Adventures - A Himitsu"


def test_split_artist_title_drops_unrelated_channel():
    title, artist = _split_artist_title("Adventures - A Himitsu", "Royalty Free Music")
    assert title == "Adventures - A Himitsu"
    assert artist == ""


def test_playlist_url_validation():
    assert _youtube_url_ok("https://music.youtube.com/playlist?list=PL123")
    assert _youtube_url_ok("https://www.youtube.com/watch?v=x&list=PL123")
    assert not _youtube_url_ok("https://music.youtube.com/watch?v=x")
    assert not _youtube_url_ok("https://example.com/playlist?list=PL123")


def test_exact_candidate_scores_high():
    source = SourceTrack(0, "Blinding Lights", "The Weeknd", 200_000, "https://example.test")
    candidate = {
        "name": "Blinding Lights",
        "artists": [{"name": "The Weeknd"}],
        "duration_ms": 200_250,
    }
    score = score_candidate(source, candidate)
    assert score["total"] > 0.98


def test_wrong_title_scores_low():
    source = SourceTrack(0, "Blinding Lights", "The Weeknd", 200_000, "https://example.test")
    candidate = {
        "name": "Save Your Tears",
        "artists": [{"name": "The Weeknd"}],
        "duration_ms": 215_000,
    }
    score = score_candidate(source, candidate)
    assert score["total"] < 0.6


def test_bulk_worker_uses_one_playlist_across_batches():
    tracks = [SourceTrack(index, f"Song {index}", "Artist", 180_000, f"https://example.test/{index}") for index in range(205)]

    class FakeSpotifyClient:
        created = 0
        added_batches: list[list[str]] = []

        def __init__(self, _session_data):
            pass

        def create_playlist(self, name, public, description):
            self.__class__.created += 1
            return {"id": "playlist-id", "name": name, "external_urls": {"spotify": "https://open.spotify.test/list"}}

        def add_items(self, playlist_id, uris):
            assert playlist_id == "playlist-id"
            self.__class__.added_batches.append(list(uris))

    def fake_extract(_url, _cookies):
        return {"title": "Big List", "count": len(tracks), "source_url": "https://example.test/list"}, tracks

    def fake_analyze(
        _client,
        batch,
        _threshold,
        progress_callback=None,
        delay_seconds=0,
        broad_search=False,
    ):
        assert broad_search is True
        results = []
        for position, source in enumerate(batch, start=1):
            results.append(
                {
                    "source": asdict(source),
                    "match": {"uri": f"spotify:track:{source.index}", "name": source.title, "artists": source.artist},
                    "score": {"total": 1.0, "title": 1.0, "artist": 1.0, "duration": 1.0},
                    "selected": True,
                    "confidence": "high",
                }
            )
            if progress_callback:
                progress_callback(position, len(batch))
        return results

    original_client = app_module.SpotifyClient
    original_extract = app_module.extract_youtube_playlist
    original_analyze = app_module.analyze_tracks
    try:
        app_module.SpotifyClient = FakeSpotifyClient
        app_module.extract_youtube_playlist = fake_extract
        app_module.analyze_tracks = fake_analyze
        session_data = {
            "bulk_job": {
                "source_url": "https://example.test/list",
                "cookies_browser": "none",
                "threshold": 0.9,
                "include_low_confidence": True,
                "requested_name": "",
                "public": False,
                "batch_size": 100,
                "next_index": 0,
                "processed": 0,
                "scanned": 0,
                "added": 0,
                "matched": 0,
                "unmatched": 0,
                "low_confidence": 0,
            }
        }
        _bulk_transfer_worker(session_data)
    finally:
        app_module.SpotifyClient = original_client
        app_module.extract_youtube_playlist = original_extract
        app_module.analyze_tracks = original_analyze

    job = session_data["bulk_job"]
    assert job["status"] == "complete"
    assert job["processed"] == 205
    assert job["added"] == 205
    assert FakeSpotifyClient.created == 1
    assert [len(batch) for batch in FakeSpotifyClient.added_batches] == [100, 100, 5]
