import logging
import telnetlib
import time
import xml.etree.ElementTree

import psycopg2

logger = logging.getLogger(__name__)

# Time to wait (seconds) between checking if we need to start something new
LOOP_WAIT = 2

# Max value of the web counter - total time will be LOOP_WAIT * WEB_WAIT
WEB_WAIT = 30

def check_web():
    """Will eventually poll another webpage to check if the schedule
    has finished runnin."""

    return True

class Database():
    """Bit of a wrapper around a psycopg2 cursor, to sanitise filenames etc."""

    def fetchall(self):
        return self.cur.fetchall()

    def execute(self, sql):
        return self.cur.execute(sql)

    def update_ident(self, filename):
        """Update last played time for an ident"""

        filename = filename.replace("'", "''")
        self.cur.execute("update idents set ident_lastplay=NOW() where ident_cgname = '{}'".format(filename))

    def update_video(self, filename):
        """Update last played time for a video"""

        filename = filename.replace("'", "''")
        self.cur.execute("update videos set video_lastplay=NOW() where video_cgname = '{}'".format(filename))

    def update_runlog(self, cmd, action):
        """Add the last run command to the runlog.
        
        Args:
            cmd: The command that has most recently been ran.
            action: video/ident/web.
        """

        cmd = cmd.replace("'", "''")
        self.cur.execute("insert into runlog (run_cmd, run_time, run_type) values ('{}', NOW(), '{}')".format(cmd, action))

    def __init__(self, cur=None):
        if cur:
            self.cur = cur
        else:
            self.conn = psycopg2.connect("dbname=huntsman user=postgres")
            self.conn.set_session(autocommit=True)
            self.cur = self.conn.cursor()

    def get_next_video(self):
        """Work out which video should be played next."""

        self.execute("select video_cgname from videos where video_lastplay is null limit 1")
        video = self.fetchall()
        if video:
            return video[0][0]

        self.execute("select video_cgname from videos order by video_lastplay asc limit 1")
        return self.fetchall()[0][0]

    def get_next_ident(self):
        """Work out which ident should be played next."""

        self.execute("select ident_cgname from idents where ident_lastplay is null limit 1")
        video = self.fetchall()
        if video:
            return video[0][0]

        self.execute("select ident_cgname from idents order by ident_lastplay asc limit 1")
        return self.fetchall()[0][0]

    def next_action(self):
        """Decide what action (video/ident/web) should be done next."""

        self.execute("select run_type from runlog order by run_time desc limit 2")

        rows = self.fetchall()

        if len(rows) == 0:
            return "ident"
        if len(rows) == 1:
            return "web"

        if rows[0][0] == "ident":
            if rows[1][0] == "web":
                return "video"
            elif rows[1][0] == "video":
                return "web"
        else:
            return "ident"

    def current_action(self):
        """Check runlog for what should be currently playing."""

        self.execute("select run_type from runlog order by run_time desc limit 1")

        rows = self.fetchall()

        if len(rows) == 0:
            return None
        else:
            return rows[0][0]

class Casparcg():

    def frames_left(self, channel=1, layer=10):
        """Work out how long is left in the currently playing ident/video.

        Returns:
            float time_left (in seconds).
        """

        cmd = "INFO {}\r\n".format(channel).encode("ascii")
        self._write(cmd)
        ret_code = self._read()
        if b"201 INFO OK" not in ret_code:
            raise Exception("Unexpected response code {}".format(ret_code))
        ret = self._read()
        tree = xml.etree.ElementTree.fromstring(ret)
        try:
            playing_file = tree.find(".//layer_{}/foreground/file/path".format(layer)).text
        except AttributeError:
            # Nothing playing
            return 0
        if playing_file[0:4] == "http":
            # webpage, so never going to end
            return 10

        times = tree.findall(".//time")
        try:
            time_tot = float(times[1].text)
        except IndexError:
            # Nothing playing
            return 0
        time_left = float(times[1].text) - float(times[0].text)

        if time_left > time_tot:
            # Probably finished
            return 0
        elif time_left < 0.05:
            return 0
        else:
            return time_left

    def clear(self, channel=1, layer=None):
        """Clear the selected channel/layer.

        Args:
            channel: channel to clear
            layer: layer to clear. Clears entire channel if not specified.
        """

        if layer:
            layer="-" + str(layer)
        else:
            layer=""

        self._write('CLEAR {}{} CLEAR\r\n'.format(channel, layer).encode("ascii"))
        ret_code = self._read()
        if b"202 CLEAR OK" not in ret_code:
            raise Exception("Unexpected response code {}".format(ret_code))
    
    def play_file(self, filename, channel=1, layer=10, loop=False):
        """Play a file.

        Args:
            filename: the file to play, as CasparCG will see it.
                (All caps, no extension)
            channel: channel to play on.
            layer: layer to play on.
            loop: True to loop the file, False to play once.
        
        Returns:
            cmd send to CasparCG.
        """

        if loop:
            loop = "loop"
        else:
            loop = ""
        cmd = "PLAY {}-{} \"{}\" CUT 1 Linear RIGHT {}\r\n".format(channel, layer, filename, loop).encode("ascii")
        self._write(cmd)
        ret_code = self._read()
        if b"202 PLAY OK" not in ret_code:
            raise Exception("Unexpected response code {}".format(ret_code))
        return cmd.decode("ascii")
    
    def play_web(self, url, channel=1, layer=10):
        """Start a webpage playing in CasparCG."""

        cmd = "PLAY {}-{} [HTML] \"{}\" CUT 1 Linear RIGHT\r\n".format(channel, layer, url)
        self._play(cmd)
        return cmd

    def play_schedule(self, url, bgvid, bgaudio, channel=1, layer=10):
        """Kick off schedule webpage, background and music."""

        cmd1 = self.play_web(url, channel, layer)
        cmd2 = self.play_file(bgaudio, channel, layer-1, loop=True)
        cmd3 = self.play_file(bgvid, channel, layer-2, loop=True)
        return cmd1 + cmd2 + cmd3

    def _play(self, cmd):
        self._write(cmd.encode("ascii"))
        ret_code = self._read()
        if b"202 PLAY OK" not in ret_code:
            raise Exception("Unexpected response code {}".format(ret_code))
    
    def _read(self):
        ret_code = None
        while not ret_code:
            try:
                ret_code = self.tel.read_until(b"\r\n", 2)
                break
            except (ConnectionAbortedError, EOFError):
                try:
                    self.tel = telnetlib.Telnet(host = host, port = port)
                except ConnectionRefusedError:
                    logger.warning("Lost connection to CasparCG, retrying in 2 seconds.")
                    time.sleep(2)
        return ret_code
    
    def _write(self, cmd):
        while True:
            try:
                self.tel.write(cmd)
                break
            except ConnectionAbortedError:
                try:
                    self.tel = telnetlib.Telnet(host = host, port = port)
                except ConnectionRefusedError:
                    logger.warning("Lost connection to CasparCG, retrying in 2 seconds.")
                    time.sleep(2)

    def __init__(self, host = None, port = None):
        self.host = host
        self.port = port
        self.tel = None

        while not self.tel:
            try:
                self.tel = telnetlib.Telnet(host = host, port = port)
            except ConnectionRefusedError:
                logger.warning("Unable to connect to {}:{}, retrying in 10 seconds.".format(host, port))
                time.sleep(10)
        self.name = 'casparcg'

def run_control(cghost, cgport, cgweb, dbcur=None):
    """Main loop that actually does the CasparCG control."""

    cg = Casparcg(cghost, cgport)
    if dbcur:
        db = Database(cur=dbcur)
    else:
        db = Database()

    action = db.next_action()
    web_count = WEB_WAIT

    logger.info("Starting Huntsman player control")
    while True:
        try:
            frames = cg.frames_left()
            if frames == 0:
                action = db.next_action()
                if action == "video":
                    next_vid = db.get_next_video()
                    logger.info("Playing {}.".format(next_vid))
                    cmd = cg.play_file(next_vid)
                    db.update_runlog(cmd, action)
                    db.update_video(next_vid)
                elif action == "web":
                    cmd = cg.play_schedule(cgweb, "SCHEDULE/ROSES-BG-BLANK", "SCHEDULE/BG-MUSIC")
                    logger.info("Playing Schedule.")
                    db.update_runlog(cmd, action)
                elif action == "ident":
                    next_ident = db.get_next_ident()
                    logger.info("Playing {}.".format(next_ident))
                    cmd = cg.play_file(next_ident)
                    db.update_runlog(cmd, action)
                    db.update_ident(next_ident)
                time.sleep(LOOP_WAIT)
            elif frames < 1:
                logger.debug("{:.2f} seconds left, waiting for {} seconds...".format(frames, LOOP_WAIT))
                time.sleep(1/25)
            elif frames < 3:
                logger.debug("{:.2f} seconds left, waiting for {} seconds...".format(frames, LOOP_WAIT))
                time.sleep(frames - 0.5)
            else:
                if db.current_action() == "web":
                    if web_count <= 0:
                        web_count = WEB_WAIT
                        cg.clear()
                        continue
                    else:
                        web_count -= 1
                    frames = web_count * LOOP_WAIT
                logger.debug("{:.2f} seconds left, waiting for {} seconds...".format(frames, LOOP_WAIT))
                time.sleep(LOOP_WAIT)
        except KeyboardInterrupt:
            break
        except:
            logger.exception("Unexpected error.")
            time.sleep(LOOP_WAIT)

if __name__ == "__main__":
    
    logging.basicConfig(
        level=logging.DEBUG, 
        format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
        datefmt='%m-%d %H:%M'
    )

    host = 'localhost'
    port = '5250'

    cg = Casparcg(host, port)
    run_control(host, port, "http://notmattandtom.co.uk:3001/")
