import os
import sqlite3
import threading
from functools import lru_cache
from json import loads
from time import time

from flask import url_for

from .config_manager import Config
from .constant import Constant
from .error import NoAccess
from .limiter import ArcLimiter
from .user import User
from .util import get_file_md5, md5


class SongFileCache:
    _local = threading.local()
    _schema_lock = threading.Lock()

    @classmethod
    def db_path(cls) -> str:
        return os.path.join(os.path.dirname(Config.SQLITE_DATABASE_PATH) or '.', 'song_cache.db')

    @classmethod
    def _conn(cls) -> sqlite3.Connection:
        conn = getattr(cls._local, 'conn', None)
        if conn is not None:
            return conn
        os.makedirs(os.path.dirname(cls.db_path()) or '.', exist_ok=True)
        conn = sqlite3.connect(cls.db_path(), isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        cls._local.conn = conn
        return conn

    @classmethod
    def ensure_schema(cls) -> None:
        with cls._schema_lock:
            c = cls._conn()
            c.execute('PRAGMA journal_mode=WAL;')
            c.execute('PRAGMA synchronous=NORMAL;')
            c.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);')
            c.execute('CREATE TABLE IF NOT EXISTS songs (song_id TEXT PRIMARY KEY, dir_mtime_ns INTEGER NOT NULL, last_scan INTEGER NOT NULL);')
            c.execute('CREATE TABLE IF NOT EXISTS files (song_id TEXT NOT NULL, file_name TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, md5 TEXT, last_seen INTEGER NOT NULL, PRIMARY KEY (song_id, file_name));')
            c.execute('CREATE INDEX IF NOT EXISTS idx_files_song_id ON files(song_id);')

    @classmethod
    def _delete_song(cls, song_id: str) -> None:
        cls.ensure_schema()
        c = cls._conn()
        c.execute('DELETE FROM files WHERE song_id=?;', (song_id,))
        c.execute('DELETE FROM songs WHERE song_id=?;', (song_id,))

    @classmethod
    def _delete_file(cls, song_id: str, file_name: str) -> None:
        cls.ensure_schema()
        c = cls._conn()
        c.execute('DELETE FROM files WHERE song_id=? AND file_name=?;', (song_id, file_name))

    @classmethod
    def sync_song(cls, song_id: str, dir_mtime_ns: int = None) -> None:
        cls.ensure_schema()
        song_dir = os.path.join(Constant.SONG_FILE_FOLDER_PATH, song_id)
        if not os.path.isdir(song_dir):
            cls._delete_song(song_id)
            return
        if dir_mtime_ns is None:
            dir_mtime_ns = os.stat(song_dir).st_mtime_ns
        c = cls._conn()
        row = c.execute('SELECT dir_mtime_ns FROM songs WHERE song_id=?;', (song_id,)).fetchone()
        if row is not None and int(row['dir_mtime_ns']) == int(dir_mtime_ns):
            return
        now = int(time())
        c.execute('INSERT OR REPLACE INTO songs(song_id, dir_mtime_ns, last_scan) VALUES(?,?,?);', (song_id, int(dir_mtime_ns), now))
        file_names = []
        try:
            for entry in os.scandir(song_dir):
                if entry.is_file() and SonglistParser.is_available_file(song_id, entry.name):
                    file_names.append(entry.name)
        except FileNotFoundError:
            cls._delete_song(song_id)
            return
        if not file_names:
            c.execute('DELETE FROM files WHERE song_id=?;', (song_id,))
            return
        placeholders = ','.join(['?'] * len(file_names))
        c.execute(f'DELETE FROM files WHERE song_id=? AND file_name NOT IN ({placeholders});', (song_id, *file_names))
        for file_name in file_names:
            path = os.path.join(song_dir, file_name)
            if not os.path.isfile(path):
                cls._delete_file(song_id, file_name)
                continue
            st = os.stat(path)
            existing = c.execute('SELECT size, mtime_ns, md5 FROM files WHERE song_id=? AND file_name=?;', (song_id, file_name)).fetchone()
            if existing is not None and int(existing['size']) == int(st.st_size) and int(existing['mtime_ns']) == int(st.st_mtime_ns):
                if Config.SONG_FILE_HASH_PRE_CALCULATE and not existing['md5']:
                    c.execute('UPDATE files SET md5=?, last_seen=? WHERE song_id=? AND file_name=?;', (get_file_md5(path), now, song_id, file_name))
                else:
                    c.execute('UPDATE files SET last_seen=? WHERE song_id=? AND file_name=?;', (now, song_id, file_name))
                continue
            md5_value = get_file_md5(path) if Config.SONG_FILE_HASH_PRE_CALCULATE else None
            c.execute('INSERT OR REPLACE INTO files(song_id, file_name, size, mtime_ns, md5, last_seen) VALUES(?,?,?,?,?,?);', (song_id, file_name, int(st.st_size), int(st.st_mtime_ns), md5_value, now))

    @classmethod
    def sync_all(cls) -> None:
        cls.ensure_schema()
        root_dir = Constant.SONG_FILE_FOLDER_PATH
        if not os.path.isdir(root_dir):
            return
        song_ids = [entry.name for entry in os.scandir(root_dir) if entry.is_dir() and entry.name != '.' and entry.name != '..']
        for song_id in song_ids:
            song_dir = os.path.join(root_dir, song_id)
            try:
                dir_mtime_ns = os.stat(song_dir).st_mtime_ns
            except FileNotFoundError:
                continue
            cls.sync_song(song_id, dir_mtime_ns)
        c = cls._conn()
        if song_ids:
            placeholders = ','.join(['?'] * len(song_ids))
            c.execute(f'DELETE FROM songs WHERE song_id NOT IN ({placeholders});', song_ids)
            c.execute(f'DELETE FROM files WHERE song_id NOT IN ({placeholders});', song_ids)
        else:
            c.execute('DELETE FROM songs;')
            c.execute('DELETE FROM files;')

    @classmethod
    def get_all_song_ids(cls, root_mtime_ns: int) -> list:
        cls.ensure_schema()
        c = cls._conn()
        meta = c.execute('SELECT value FROM meta WHERE key="root_mtime_ns";').fetchone()
        if meta is None or int(meta['value']) != int(root_mtime_ns):
            cls.sync_all()
            c.execute('INSERT OR REPLACE INTO meta(key, value) VALUES(?,?);', ('root_mtime_ns', str(int(root_mtime_ns))))
        return [row['song_id'] for row in c.execute('SELECT song_id FROM songs ORDER BY song_id;').fetchall()]

    @classmethod
    def get_song_file_names(cls, song_id: str, dir_mtime_ns: int) -> list:
        cls.ensure_schema()
        c = cls._conn()
        row = c.execute('SELECT dir_mtime_ns FROM songs WHERE song_id=?;', (song_id,)).fetchone()
        if row is None or int(row['dir_mtime_ns']) != int(dir_mtime_ns):
            cls.sync_song(song_id, dir_mtime_ns)
        return [r['file_name'] for r in c.execute('SELECT file_name FROM files WHERE song_id=? ORDER BY file_name;', (song_id,)).fetchall()]

    @classmethod
    def get_song_file_md5(cls, song_id: str, file_name: str, file_mtime_ns: int, file_size: int) -> str:
        cls.ensure_schema()
        path = os.path.join(Constant.SONG_FILE_FOLDER_PATH, song_id, file_name)
        if not os.path.isfile(path):
            cls._delete_file(song_id, file_name)
            return None
        c = cls._conn()
        row = c.execute('SELECT size, mtime_ns, md5 FROM files WHERE song_id=? AND file_name=?;', (song_id, file_name)).fetchone()
        now = int(time())
        if row is not None and int(row['size']) == int(file_size) and int(row['mtime_ns']) == int(file_mtime_ns) and row['md5']:
            c.execute('UPDATE files SET last_seen=? WHERE song_id=? AND file_name=?;', (now, song_id, file_name))
            return row['md5']
        md5_value = get_file_md5(path)
        c.execute('INSERT OR REPLACE INTO files(song_id, file_name, size, mtime_ns, md5, last_seen) VALUES(?,?,?,?,?,?);', (song_id, file_name, int(file_size), int(file_mtime_ns), md5_value, now))
        song_dir = os.path.join(Constant.SONG_FILE_FOLDER_PATH, song_id)
        if os.path.isdir(song_dir):
            try:
                c.execute('INSERT OR IGNORE INTO songs(song_id, dir_mtime_ns, last_scan) VALUES(?,?,?);', (song_id, int(os.stat(song_dir).st_mtime_ns), now))
            except FileNotFoundError:
                pass
        return md5_value


@lru_cache(maxsize=8192)
def _get_song_file_md5_cached(song_id: str, file_name: str, file_mtime_ns: int, file_size: int) -> str:
    return SongFileCache.get_song_file_md5(song_id, file_name, file_mtime_ns, file_size)


def get_song_file_md5(song_id: str, file_name: str) -> str:
    path = os.path.join(Constant.SONG_FILE_FOLDER_PATH, song_id, file_name)
    if not os.path.isfile(path):
        SongFileCache._delete_file(song_id, file_name)
        return None
    st = os.stat(path)
    return _get_song_file_md5_cached(song_id, file_name, int(st.st_mtime_ns), int(st.st_size))


class SonglistParser:
    '''songlist文件解析器'''

    FILE_NAMES = ['0.aff', '1.aff', '2.aff', '3.aff', '4.aff',
                  'base.ogg', '3.ogg', 'video.mp4', 'video_audio.ogg', 'video_720.mp4', 'video_1080.mp4']

    has_songlist = False
    songs: dict = {}  # {song_id: value, ...}
    # value: bitmap

    pack_info: 'dict[str, set]' = {}  # {pack_id: {song_id, ...}, ...}
    free_songs: set = set()  # {song_id, ...}
    world_songs: set = set()  # {world_song_id, ...}

    def __init__(self, path=Constant.SONGLIST_FILE_PATH) -> None:
        self.path = path
        self.data: list = []
        self.parse()

    @staticmethod
    def is_available_file(song_id: str, file_name: str) -> bool:
        '''判断文件是否允许被下载'''
        if song_id not in SonglistParser.songs:
            # songlist没有，则只限制文件名
            return file_name in SonglistParser.FILE_NAMES
        rule = SonglistParser.songs[song_id]
        for i, v in enumerate(SonglistParser.FILE_NAMES):
            if file_name == v and rule & (1 << i) != 0:
                return True
        return False

    @staticmethod
    def get_user_unlocks(user) -> set:
        '''user: UserInfo类或子类的实例'''
        x = SonglistParser
        if user is None:
            return set()

        r = set()
        for i in user.packs:
            if i in x.pack_info:
                r.update(x.pack_info[i])

        if Constant.SINGLE_PACK_NAME in x.pack_info:
            r.update(x.pack_info[Constant.SINGLE_PACK_NAME]
                     & set(user.singles))
        r.update(set(i if i[-1] != '3' else i[:-1]
                     for i in (x.world_songs & set(user.world_songs))))
        r.update(x.free_songs)

        return r

    def parse_one(self, song: dict) -> dict:
        '''解析单个歌曲'''
        # TODO: byd_local_unlock ???
        if not 'id' in song:
            return {}
        r = 0
        if 'remote_dl' in song and song['remote_dl']:
            r |= 32
            for i in song.get('difficulties', []):
                if i['ratingClass'] == 3 and i.get('audioOverride', False):
                    r |= 64
                r |= 1 << i['ratingClass']
        else: # 針對remote_dl為False時BYD難度強制下載的處理
            for i in song.get('difficulties', []):
                if i['ratingClass'] == 3:
                    r |= 8
                    if i.get('audioOverride', False):
                        r |= 64

        for extra_file in song.get('additional_files', []):
            x = extra_file['file_name']
            if x == SonglistParser.FILE_NAMES[7]:
                r |= 128
            elif x == SonglistParser.FILE_NAMES[8]:
                r |= 256
            elif x == SonglistParser.FILE_NAMES[9]:
                r |= 512
            elif x == SonglistParser.FILE_NAMES[10]:
                r |= 1024

        return {song['id']: r}

    def parse_one_unlock(self, song: dict) -> None:
        '''解析单个歌曲解锁方式'''
        if not 'id' in song or not 'set' in song or not 'purchase' in song:
            return {}
        x = SonglistParser
        if Constant.FREE_PACK_NAME == song['set']:
            if any(i['ratingClass'] == 3 for i in song.get('difficulties', [])):
                x.world_songs.add(song['id'] + '3')
            x.free_songs.add(song['id'])
            return None

        if song.get('world_unlock', False):
            x.world_songs.add(song['id'])

        if song['purchase'] == '':
            return None

        x.pack_info.setdefault(song['set'], set()).add(song['id'])

    def parse(self) -> None:
        '''解析songlist文件'''
        if not os.path.isfile(self.path):
            return
        with open(self.path, 'rb') as f:
            self.data = loads(f.read()).get('songs', [])
        self.has_songlist = True
        for x in self.data:
            self.songs.update(self.parse_one(x))
            self.parse_one_unlock(x)


class UserDownload:
    '''
        用户下载类

        properties: `user` - `UserInfo`类或子类的实例
    '''

    limiter = ArcLimiter(
        str(Constant.DOWNLOAD_TIMES_LIMIT) + '/day', 'download')

    def __init__(self, c_m=None, user=None) -> None:
        self.c_m = c_m
        self.user = user

        self.song_id: str = None
        self.file_name: str = None

        self.token: str = None
        self.token_time: int = None

    @property
    def is_limited(self) -> bool:
        '''是否达到用户最大下载量'''
        if self.user is None:
            self.select_for_check()

        return not self.limiter.test(str(self.user.user_id))

    @property
    def is_valid(self) -> bool:
        '''链接是否有效且未过期'''
        if self.token_time is None:
            self.select_for_check()
        return int(time()) - self.token_time <= Constant.DOWNLOAD_TIME_GAP_LIMIT

    def download_hit(self) -> bool:
        '''下载次数+1，返回成功与否bool值'''
        return self.limiter.hit(str(self.user.user_id))

    def select_for_check(self) -> None:
        '''利用token、song_id、file_name查询其它信息'''
        self.c_m.execute('''select user_id, time from download_token where song_id=? and file_name=? and token = ? limit 1;''',
                         (self.song_id, self.file_name, self.token))

        x = self.c_m.fetchone()
        if not x:
            raise NoAccess('The token `%s` is not valid.' %
                           self.token, status=403)
        self.user = User()
        self.user.user_id = x[0]
        self.token_time = x[1]

    def generate_token(self) -> None:
        self.token_time = int(time())
        self.token = md5(str(self.user.user_id) + self.song_id +
                         self.file_name + str(self.token_time) + str(os.urandom(8)))

    def insert_download_token(self) -> None:
        '''将数据插入数据库，让这个下载链接可用'''
        self.c_m.execute('''insert or replace into download_token values(:a,:b,:c,:d,:e)''', {
            'a': self.user.user_id, 'b': self.song_id, 'c': self.file_name, 'd': self.token, 'e': self.token_time})

    @property
    def url(self) -> str:
        '''生成下载链接'''
        if self.token is None:
            self.generate_token()
            # self.insert_download_token() # 循环插入速度慢，改为executemany
        if Constant.DOWNLOAD_LINK_PREFIX:
            prefix = Constant.DOWNLOAD_LINK_PREFIX
            if prefix[-1] != '/':
                prefix += '/'
            return f'{prefix}{self.song_id}/{self.file_name}?t={self.token}'
        return url_for('download', file_path=f'{self.song_id}/{self.file_name}', t=self.token, _external=True)

    @property
    def hash(self) -> str:
        return get_song_file_md5(self.song_id, self.file_name)


class DownloadList(UserDownload):
    '''
        下载列表类
        properties: `user` - `User`类或子类的实例
    '''

    def __init__(self, c_m=None, user=None) -> None:
        super().__init__(c_m, user)

        self.song_ids: list = None
        self.url_flag: bool = None

        self.downloads: list = []
        self.urls: dict = {}

    @classmethod
    def initialize_cache(cls) -> None:
        '''初始化歌曲数据缓存，包括md5、文件目录遍历、解析songlist'''
        SonglistParser()
        SongFileCache.ensure_schema()
        SongFileCache.sync_all()

    @staticmethod
    def clear_all_cache() -> None:
        '''清除所有歌曲文件有关缓存'''
        _get_song_file_md5_cached.cache_clear()
        DownloadList._get_cached_song_file_names.cache_clear()
        DownloadList._get_all_cached_song_ids.cache_clear()
        SonglistParser.songs = {}
        SonglistParser.pack_info = {}
        SonglistParser.free_songs = set()
        SonglistParser.world_songs = set()
        SonglistParser.has_songlist = False

    def clear_download_token(self) -> None:
        '''清除过期下载链接'''
        self.c_m.execute('''delete from download_token where time<?''',
                         (int(time()) - Constant.DOWNLOAD_TIME_GAP_LIMIT,))

    def insert_download_tokens(self) -> None:
        '''插入所有下载链接'''
        self.c_m.executemany('''insert or replace into download_token values(?,?,?,?,?)''', [(
            self.user.user_id, x.song_id, x.file_name, x.token, x.token_time) for x in self.downloads])

    @staticmethod
    @lru_cache(maxsize=2048)
    def _get_cached_song_file_names(song_id: str, dir_mtime_ns: int) -> list:
        '''获取一个歌曲文件夹下的所有合法文件名，有lru缓存'''
        return SongFileCache.get_song_file_names(song_id, dir_mtime_ns)

    @staticmethod
    def get_one_song_file_names(song_id: str) -> list:
        song_dir = os.path.join(Constant.SONG_FILE_FOLDER_PATH, song_id)
        if not os.path.isdir(song_dir):
            SongFileCache._delete_song(song_id)
            return []
        try:
            dir_mtime_ns = os.stat(song_dir).st_mtime_ns
        except FileNotFoundError:
            SongFileCache._delete_song(song_id)
            return []
        return DownloadList._get_cached_song_file_names(song_id, int(dir_mtime_ns))

    def add_one_song(self, song_id: str) -> None:

        re = {}
        for i in self.get_one_song_file_names(song_id):
            x = UserDownload(self.c_m, self.user)
            self.downloads.append(x)
            x.song_id = song_id
            x.file_name = i
            if i == 'base.ogg':
                if 'audio' not in re:
                    re['audio'] = {}

                re['audio']["checksum"] = x.hash
                if self.url_flag:
                    re['audio']["url"] = x.url
            elif i == '3.ogg':
                if 'audio' not in re:
                    re['audio'] = {}

                if self.url_flag:
                    re['audio']['3'] = {"checksum": x.hash, "url": x.url}
                else:
                    re['audio']['3'] = {"checksum": x.hash}
            elif i in ('video.mp4', 'video_audio.ogg', 'video_720.mp4', 'video_1080.mp4'):
                if 'additional_files' not in re:
                    re['additional_files'] = []

                if self.url_flag:
                    re['additional_files'].append(
                        {"checksum": x.hash, "url": x.url, 'file_name': i})
                else:
                    re['additional_files'].append(
                        {"checksum": x.hash, 'file_name': i})
                # 有参数 requirement 作用未知
            else:
                if 'chart' not in re:
                    re['chart'] = {}

                if self.url_flag:
                    re['chart'][i[0]] = {"checksum": x.hash, "url": x.url}
                else:
                    re['chart'][i[0]] = {"checksum": x.hash}

        self.urls.update({song_id: re})

    @staticmethod
    @lru_cache()
    def _get_all_cached_song_ids(root_mtime_ns: int) -> list:
        '''获取全歌曲文件夹列表，有lru缓存'''
        return SongFileCache.get_all_song_ids(root_mtime_ns)

    @staticmethod
    def get_all_song_ids() -> list:
        root_dir = Constant.SONG_FILE_FOLDER_PATH
        if not os.path.isdir(root_dir):
            return []
        try:
            root_mtime_ns = os.stat(root_dir).st_mtime_ns
        except FileNotFoundError:
            return []
        return DownloadList._get_all_cached_song_ids(int(root_mtime_ns))

    def add_songs(self, song_ids: list = None) -> None:
        '''添加一个或多个歌曲到下载列表，若`song_ids`为空，则添加所有歌曲'''
        if song_ids is not None:
            self.song_ids = song_ids

        if not self.song_ids:
            self.song_ids = self.get_all_song_ids()
            if Config.DOWNLOAD_FORBID_WHEN_NO_ITEM and SonglistParser.has_songlist:
                # 没有歌曲时不允许下载
                self.song_ids = list(SonglistParser.get_user_unlocks(
                    self.user) & set(self.song_ids))

            for i in self.song_ids:
                self.add_one_song(i)
        else:
            if Config.DOWNLOAD_FORBID_WHEN_NO_ITEM and SonglistParser.has_songlist:
                # 没有歌曲时不允许下载
                self.song_ids = list(SonglistParser.get_user_unlocks(
                    self.user) & set(self.song_ids))

            for i in self.song_ids:
                if os.path.isdir(os.path.join(Constant.SONG_FILE_FOLDER_PATH, i)):
                    self.add_one_song(i)

        if self.url_flag:
            self.clear_download_token()
            self.insert_download_tokens()
