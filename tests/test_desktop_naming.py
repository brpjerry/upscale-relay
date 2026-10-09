"""Readable file names ("Simplify file names"), from a real anime library."""
import os

import pytest

from desktop_client.naming import display_name

NAMES = [
    # Fansub releases: group, " - NN", release details in brackets and parentheses.
    ("[SubsPlease] Honzuki no Gekokujou S4 - 13 (1080p) [A4FE0990].mkv", "13 · Honzuki no Gekokujou S4"),
    ("[SubsPlease] Kusuriya no Hitorigoto - 49v2 (1080p) [F14E89B9].mkv", "49 · Kusuriya no Hitorigoto"),
    ("[SubsPlease] Kamitsubaki-shi Kensetsuchuu. - 07 (1080p) [E3D8BE48].mkv", "07 · Kamitsubaki-shi Kensetsuchuu."),
    ("[Tsundere] A-Channel - SP01 [BDRip h264 1920x1080 FLAC][A8350932].mkv", "SP01 · A-Channel"),
    ("[MTBB] Hyouka - 11.5 (OVA) (BD 1080p) [9199C7FF].mkv", "11.5 · Hyouka (OVA)"),
    ("[ak-Submarines] Girls und Panzer - MLLSD - 11 [WEB 1080p][C450169D].mkv", "11 · Girls und Panzer - MLLSD"),
    # Underscores, and a number at the end.
    ("[Coalgirls]_Baccano!_01_(1280x720_Blu-ray_FLAC)_[09F341E5].mkv", "01 · Baccano!"),
    ("[EG]Gurren_Lagann_21_BD_V2(720p_10bit)[12A2B1F7].mkv", "21 · Gurren Lagann"),
    ("[CH]_Kill_la_Kill_EP_16_[0CA9FC5D].mkv", "16 · Kill la Kill"),
    ("[DrK] Sora yori mo Tooi Basho 05 [BDRip 1920x1080 x264 FLAC] [E139E3F5].mkv", "05 · Sora yori mo Tooi Basho"),
    # Scene names: dots for spaces, release details after the episode.
    ("Chainsmoker.Cat.S01E03.1080p.NF.WEB-DL.AAC2.0.H.264-VARYG.mkv", "S01E03 · Chainsmoker Cat"),
    ("Ichijyoma.Mankitsu.Gurashi.S01E09.Marika.the.Target.1080p.AMZN.WEB-DL.AAC2.0.H.264-VARYG.mkv",
     "S01E09 · Ichijyoma Mankitsu Gurashi - Marika the Target"),
    ("Izetta.The.Last.Witch.S01E01.1080p-Hi10p.BluRay.FLAC5.1.x264-CTR.[EB292A33].mkv", "S01E01 · Izetta The Last Witch"),
    ("Love.Live!.Nijigasaki.High.School.Idol.Club.Final.Chapter.Part.1.2024.1080p.BluRay.Remux.AVC.FLAC5.1-Headpatter.mkv",
     "Love Live! Nijigasaki High School Idol Club Final Chapter Part 1"),
    # Sonarr-style: a year, an absolute number and a release group after the brackets.
    ("Akiba Maid War (2022) - S01E02 - 002 - Gambling Adoracalypse Yumechi [Bluray-1080p][8bit][x264][FLAC 2.0][JA+EN]-CW.mkv",
     "S01E02 · Akiba Maid War - Gambling Adoracalypse Yumechi"),
    ("Watashi ni Tenshi ga Maiorita! - 1x07 - I Don't Understand What Mya-Nee is Saying [BD-1080p x265 FLAC2.0] [Smoke] [82265AB7].mkv",
     "S01E07 · Watashi ni Tenshi ga Maiorita! - I Don't Understand What Mya-Nee is Saying"),
    # The episode, or the whole title, only inside brackets.
    ("[VCB-Studio] Love Live! S2 [03][Ma10p_1080p][x265_flac].mkv", "03 · Love Live! S2"),
    ("[DBD-Raws][Love Live! Superstar!! S1][Music][12][1080P][BDRip][HEVC-10bit][FLAC].mkv",
     "12 · Love Live! Superstar!! S1 - Music"),
    # Episode first, or nothing but the episode.
    ("01 - Equivalent Exchange.mkv", "01 · Equivalent Exchange"),
    ("Episode 04.mkv", "04"),
    ("S01E07.mkv", "S01E07"),
    # Parentheses that are part of the title stay; release details go.
    ("[Beatrice-Raws] Evangelion 2.0 You Can (Not) Advance (CM 01) [BDRip 1920x1080 HEVC TrueHD].mkv",
     "Evangelion 2.0 You Can (Not) Advance (CM 01)"),
    ("[Furenzu] Kemono Friends NCOP (Ep. 06) - Youkoso Japari Park e (BD 720p FLAC) [9AC8C2AE].mkv",
     "06 · Kemono Friends NCOP - Youkoso Japari Park e"),
    # No episode: only cleaned.
    ("[MTBB] K-ON! the Movie (2011) (BD 1080p) [805DBF12].mkv", "K-ON! the Movie"),
    ("Menu Vol 4.mkv", "Menu Vol 4"),
    ("GAME [quUbmzZ2KQA].mkv", "GAME"),
]


@pytest.mark.parametrize("filename,shown", NAMES)
def test_display_names_put_the_episode_first_without_release_details(filename, shown):
    assert display_name(filename) == shown


def test_a_name_with_nothing_left_stays_as_it_is():
    assert display_name("[1080p].mkv") == "[1080p]"


def test_setting_changes_only_the_shown_name(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtWidgets import QApplication, QFileSystemModel
    from desktop_client.browser_state import PROGRESS_ROLE, LocalLibraryProxy
    from desktop_client.history import HistoryStore, source_key
    QApplication.instance() or QApplication([])
    name = "[SubsPlease] Tenkosaki - 03 (1080p) [61ABF132].mkv"
    (tmp_path / name).write_bytes(b"")
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat)
    history = HistoryStore(settings)
    history.save(source_key("uplink", str(tmp_path / name)), 710.0, 1420.0)
    model = QFileSystemModel()
    model.setRootPath(str(tmp_path))
    proxy = LocalLibraryProxy(history)
    proxy.setSourceModel(model)
    from PySide6.QtTest import QTest
    for _ in range(100):
        index = proxy.mapFromSource(model.index(str(tmp_path / name)))
        if index.isValid():
            break
        QTest.qWait(20)
    assert index.data(Qt.DisplayRole) == name
    proxy.simplify_names = True
    assert index.data(Qt.DisplayRole) == "03 · Tenkosaki"
    assert index.data(Qt.ToolTipRole).startswith(name)  # the real name stays at hand
    assert index.data(PROGRESS_ROLE) == "50%"
