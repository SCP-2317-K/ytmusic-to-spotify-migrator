from app import SourceTrack, _clean_source_title, _split_artist_title, _youtube_url_ok, normalize_artist, normalize_text, score_candidate


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
