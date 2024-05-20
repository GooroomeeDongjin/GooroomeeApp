import time
import cv2
from PyQt5 import QtCore
from PyQt5.QtCore import pyqtSlot

from afy.utils import crop, resize
from afy.arguments import opt

from SPIGA.spiga.gooroomee_spiga.spiga_wrapper import SPIGAWrapper
from gooroomee.grm_defs import GrmParentThread, IMAGE_SIZE, ModeType
from gooroomee.grm_packet import BINWrapper, TYPE_INDEX
from gooroomee.grm_predictor import GRMPredictor
from gooroomee.grm_queue import GRMQueue


def get_current_time_ms():
    return round(time.time() * 1000)


class EncodeVideoPacketWorker(GrmParentThread):
    replace_image_frame = None
    frame_proportion = 0.9
    frame_offset_x = 0
    frame_offset_y = 0

    def __init__(self,
                 p_video_capture_queue,
                 p_send_video_queue,
                 p_get_worker_seq_num,
                 p_get_worker_ssrc,
                 p_get_grm_mode_type):
        super().__init__()
        self.width = 0
        self.height = 0
        self.sent_key_frame = False
        self.bin_wrapper = BINWrapper()
        self.video_capture_queue: GRMQueue = p_video_capture_queue
        self.send_video_queue: GRMQueue = p_send_video_queue
        self.get_worker_seq_num = p_get_worker_seq_num
        self.get_worker_ssrc = p_get_worker_ssrc
        self.get_grm_mode_type = p_get_grm_mode_type
        self.request_send_key_frame_flag: bool = False
        self.request_recv_key_frame_flag: bool = False
        self.connect_flag: bool = False
        self.avatar_kp = None
        self.predictor = None
        self.spigaEncodeWrapper = None

    def create_avatarify(self):
        if self.predictor is None:
            predictor_args = {
                'config_path': opt.config,
                'checkpoint_path': opt.checkpoint,
                'relative': opt.relative,
                'adapt_movement_scale': opt.adapt_scale,
                'enc_downscale': opt.enc_downscale
            }

            print(f'create_avatarify ENCODER')
            self.predictor = GRMPredictor(
                **predictor_args
            )

    def change_avatar(self, new_avatar):
        print(f'encoder. change_avatar, resolution:{new_avatar.shape[0]} x {new_avatar.shape[1]}')
        self.avatar_kp = self.predictor.get_frame_kp(new_avatar)
        avatar = new_avatar
        self.predictor.set_source_image(avatar)
        self.predictor.reset_frames()

    def create_spiga(self):
        if self.spigaEncodeWrapper is None:
            print(f'create_spiga ENCODER')
            self.spigaEncodeWrapper = SPIGAWrapper((IMAGE_SIZE, IMAGE_SIZE, 3))

    def set_replace_image_frame(self, frame):
        if frame is None:
            self.replace_image_frame = None
        else:
            self.replace_image_frame = frame.copy()
        self.request_send_key_frame()

    def set_connect(self, p_connect_flag: bool):
        self.connect_flag = p_connect_flag
        print(f"CaptureFrameWorker connect:{self.connect_flag}")

    def request_send_key_frame(self):
        print("request send_key_frame")
        self.request_send_key_frame_flag = True

    def request_recv_key_frame(self):
        print("request recv_key_frame")
        self.request_recv_key_frame_flag = True

    def send_key_frame(self, frame_orig):
        if frame_orig is None:
            print("failed to make key_frame")
            return False

        img = None
        if self.replace_image_frame is not None:
            img = cv2.cvtColor(self.replace_image_frame, cv2.COLOR_RGB2BGR)
        else:
            avatar_frame = frame_orig[..., ::-1]
            avatar_frame, (self.frame_offset_x, self.frame_offset_y) = crop(avatar_frame,
                                                                            p=self.frame_proportion,
                                                                            offset_x=self.frame_offset_x,
                                                                            offset_y=self.frame_offset_y)
            img = resize(avatar_frame, (IMAGE_SIZE, IMAGE_SIZE))[..., :3]

        if img is not None:
            new_avatar = img.copy()
            self.change_avatar(new_avatar)

            key_frame = cv2.imencode('.jpg', img)
            key_frame_bin_data = self.bin_wrapper.to_bin_key_frame(key_frame[1])

            self.video_capture_queue.clear()
            # self.send_video_queue.clear()

            bin_data = self.bin_wrapper.to_bin_wrap_common_header(timestamp=get_current_time_ms(),
                                                                  seq_num=self.get_worker_seq_num(),
                                                                  ssrc=self.get_worker_ssrc(),
                                                                  mediatype=TYPE_INDEX.TYPE_VIDEO,
                                                                  bindata=key_frame_bin_data)

            self.send_video_queue.put(bin_data)
            print(
                f'send_key_frame. len:[{len(key_frame_bin_data)}], resolution:{img.shape[0]} x {img.shape[1]}')

            self.sent_key_frame = True
            return True

        return False

    def run(self):
        while self.alive:
            self.sent_key_frame = False

            while self.running:
                # print(f"recv video queue read .....")
                while self.video_capture_queue.length() > 0:
                    # print(f"video_capture_queue ..... length:{self.video_capture_queue.length()}")
                    frame = self.video_capture_queue.pop()

                    if type(frame) is bytes:
                        print(f'EncodeVideoPacketWorker. frame type is invalid')
                        continue

                    if frame is None or self.join_flag is False:
                        time.sleep(0.1)
                        continue

                    if self.request_send_key_frame_flag is True:
                        self.request_send_key_frame_flag = False
                        if self.get_grm_mode_type() == ModeType.KDM:
                            pass
                        else:
                            if self.send_key_frame(frame) is False:
                                self.request_send_key_frame_flag = True
                            continue

                    video_bin_data = None
                    if self.request_recv_key_frame_flag is True:
                        self.request_recv_key_frame_flag = False
                        video_bin_data = self.bin_wrapper.to_bin_request_key_frame()

                    if video_bin_data is None:
                        if self.get_grm_mode_type() == ModeType.KDM:
                            features_tracker, features_spiga = self.spigaEncodeWrapper.encode(frame)
                            if features_tracker is not None and features_spiga is not None:
                                video_bin_data = self.bin_wrapper.to_bin_features(frame,
                                                                                  features_tracker,
                                                                                  features_spiga)
                        else:
                            if self.sent_key_frame is True:
                                kp_norm = self.predictor.encoding(frame)
                                video_bin_data = self.bin_wrapper.to_bin_kp_norm(kp_norm)

                    if video_bin_data is not None:
                        video_bin_data = self.bin_wrapper.to_bin_wrap_common_header(
                            timestamp=get_current_time_ms(),
                            seq_num=self.get_worker_seq_num(),
                            ssrc=self.get_worker_ssrc(),
                            mediatype=TYPE_INDEX.TYPE_VIDEO,
                            bindata=video_bin_data)

                        self.send_video_queue.put(video_bin_data)

                    time.sleep(0.001)
                time.sleep(0.1)
            time.sleep(0.1)

        print("Stop EncodeVideoPacketWorker")
        self.terminated = True
        # self.terminate()