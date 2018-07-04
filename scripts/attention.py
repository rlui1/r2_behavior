#!/usr/bin/env python
import rospy
import tf
import time
import threading
import math
import operator
import random
import numpy as np
import json
import os
import yaml
import random
import pprint
from dynamic_reconfigure.server import Server
import dynamic_reconfigure.client
from r2_behavior.cfg import AttentionConfig
from r2_behavior.msg import APILookAt
from blender_api_msgs.msg import Target, EmotionState, SetGesture
from std_msgs.msg import String, Float64, UInt8
from geometry_msgs.msg import Point
from r2_perception.msg import State, Face, SalientPoint
from hr_msgs.msg import pau
from geometry_msgs.msg import Point,PointStamped
# Attention regions
from performances.nodes import attention as AttentionRegion
import logging

logger = logging.getLogger('hr.r2_behavior.attention')

# in interactive settings with people, the EyeContact machine is used to define specific states for eye contact
# this is purely mechanical, so it follows a very strict control logic; the overall state machines controls which
# eyecontact mode is actually used by switching the eyecontact state
class EyeContact:
    IDLE = 0  # don't make eye contact
    LEFT_EYE = 1  # look at left eye
    RIGHT_EYE = 2  # look at right eye
    BOTH_EYES = 3  # switch between both eyes
    TRIANGLE = 4  # switch between eyes and mouth


# the lookat machine is the lowest level and has the robot look at specific things: saliency, hands, faces
# this is purely mechanical, so it follows a very strict control logic; the overall state machines controls
# where the robot looks at by switching the lookat state
class LookAt:
    IDLE = 0  # look at nothing in particular
    AVOID = 1  # actively avoid looking at face, hand or saliency
    SALIENCY = 2  # look at saliency and switch
    ONE_FACE = 3  # look at single face and make eye contact
    ALL_FACES = 4  # look at all faces, make eye contact and switch
    REGION = 5  # look at the region and switch
    HOLD = 6 # Do not move the head while in  this state
    NEAREST_FACE = 7  # only look at face closest to the robot. This ensures robot do not get distracted by other people around

# params: current face


# the mirroring machine is the lowest level and has the robot mirror the face it is currently looking at
# this is purely mechanical, so it follows a very strict control logic; the overall state machine controls which mirroring more is actually used by switching the mirroring state
class Mirroring:
    IDLE = 0  # no mirroring
    EYEBROWS = 1  # mirror the eyebrows only
    EYELIDS = 2  # mirror the blinking only
    EYES = 3  # mirror eyebrows and eyelids
    MOUTH = 4  # mirror mouth opening
    MOUTH_EYEBROWS = 5  # mirror mouth and eyebrows
    MOUTH_EYELIDS = 6  # mirror mouth and eyelids
    ALL = 7  # mirror everything


# params: eyebrows magnitude, eyelid magnitude, mouth magnitude


# the gaze machine is the lowest level and defines the robot head+gaze behavior
# this is purely mechanical, so it follows a very strict control logic; the overall state machine controls which gaze mode is actually used by switching the gaze state
class Gaze:
    GAZE_ONLY = 0  # only gaze
    HEAD_ONLY = 1  # only head
    GAZE_AND_HEAD = 2  # gaze and head at the same time
    GAZE_LEADS_HEAD = 3  # gaze first, and after some time have head follow
    HEAD_LEADS_GAZE = 4  # head first, and after some time have gaze follow


# params: gaze delay, gaze speed
REGIONS = {
    0: 'audience',  # Audience region selected
    1: 'main',  # Presenter (speaker) region
    2: 'specific'   # Co-presenter or some other region for specific setting
}

# awareness: saliency, hands, faces, motion sensors

class Attention:
    def InitSaliencyCounter(self):
        self.saliency_counter = random.randint(int(self.saliency_time_min * self.synthesizer_rate),
                                               int(self.saliency_time_max * self.synthesizer_rate))

    def InitFacesCounter(self):
        self.faces_counter = random.randint(int(self.faces_time_min * self.synthesizer_rate),
                                            int(self.faces_time_max * self.synthesizer_rate))

    def InitEyesCounter(self):
        self.eyes_counter = random.randint(int(self.eyes_time_min * self.synthesizer_rate),
                                           int(self.eyes_time_max * self.synthesizer_rate))

    def InitRegionCounter(self):
        self.region_counter = random.randint(int(self.region_time_min * self.synthesizer_rate),
                                             int(self.region_time_max * self.synthesizer_rate))

    def InitAllFacesStartCounter(self):
        self.all_faces_start_counter = random.randint(int(self.all_faces_start_time_min * self.synthesizer_rate),
                                                      int(self.all_faces_start_time_max * self.synthesizer_rate))

    def InitAllFacesDurationCounter(self):
        self.all_faces_duration_counter = random.randint(int(self.all_faces_duration_min * self.synthesizer_rate),
                                                         int(self.all_faces_duration_max * self.synthesizer_rate))

    def __init__(self):

        # create lock
        self.lock = threading.Lock()

        self.robot_name = rospy.get_param("/robot_name")

        # setup face, hand and saliency structures
        self.state = None
        self.current_face_index = -1  # index to current face
        self.wanted_face_id = 0  # ID for wanted face
        self.current_saliency_index = -1  # index of current saliency vector
        self.current_eye = 0  # current eye (0 = left, 1 = right, 2 = mouth)
        self.interrupted_state = LookAt.IDLE  # which state was interrupted to look at all faces
        self.interrupting = False  # LookAt state is currently interrupted to look at all faces

        self.gaze_delay_counter = 0  # delay counter after with gaze or head follows head or gaze
        self.gaze_pos = None  # current gaze position

        self.tf_listener = tf.TransformListener(False, rospy.Duration.from_sec(1))

        # setup dynamic reconfigure parameters
        # TODO remove should be initialized with initial ser
        self.enable_flag = True
        self.synthesizer_rate = 10.0
        self.keep_time = 1.0
        self.saliency_time_min = 0.1
        self.saliency_time_max = 3.0
        self.faces_time_min = 0.1
        self.faces_time_max = 3.0
        self.eyes_time_min = 0.1
        self.eyes_time_max = 3.0
        self.region_time_min = 0.1
        self.region_time_max = 3.0
        self.gesture_time_min = 0.1
        self.gesture_time_max = 3.0
        self.expression_time_min = 0.1
        self.expression_time_max = 3.0
        self.InitSaliencyCounter()
        self.InitFacesCounter()
        self.InitEyesCounter()
        self.InitRegionCounter()
        self.face_state_decay = 2.0
        self.gaze_delay = 1.0
        self.gaze_speed = 0.5
        self.interrupt_to_all_faces = False
        self.all_faces_start_time_min = 4.0
        self.all_faces_start_time_max = 6.0
        self.all_faces_duration_min = 2.0
        self.all_faces_duration_max = 4.0
        self.InitAllFacesStartCounter()
        self.InitAllFacesDurationCounter()
        self.eyecontact = EyeContact.IDLE
        self.lookat = LookAt.IDLE
        self.mirroring = Mirroring.IDLE
        self.gaze = Gaze.GAZE_ONLY
        self.attetntion_region = 0
        self.head_speed = 1
        rospy.Subscriber('/{}/perception/state'.format(self.robot_name), State, self.HandleState)

        self.head_focus_pub = rospy.Publisher('/blender_api/set_face_target', Target, queue_size=1)
        self.gaze_focus_pub = rospy.Publisher('/blender_api/set_gaze_target', Target, queue_size=1)
        self.expressions_pub = rospy.Publisher('/blender_api/set_emotion_state', EmotionState, queue_size=1)
        self.gestures_pub = rospy.Publisher('/blender_api/set_gesture', SetGesture, queue_size=1)
        self.animationmode_pub = rospy.Publisher('/blender_api/set_animation_mode', UInt8, queue_size=1)
        self.setpau_pub = rospy.Publisher('/blender_api/set_pau', pau, queue_size=1)

        self.hand_events_pub = rospy.Publisher('/hand_events', String, queue_size=1)

        # API calls (when the overall state is not automatically setting these values via dynamic reconfigure)
        self.eyecontact_sub = rospy.Subscriber('/behavior/attention/api/eyecontact',UInt8, self.HandleEyeContact)
        self.lookat_sub = rospy.Subscriber('/behavior/attention/api/lookat',APILookAt, self.HandleLookAt)
        self.mirroring_sub = rospy.Subscriber('/behavior/attention/api/mirroring',UInt8, self.HandleMirroring)
        self.gaze_sub = rospy.Subscriber('/behavior/attention/api/gaze',UInt8, self.HandleGaze)

        # start dynamic reconfigure server
        self.configs_init = False
        self.config_server = Server(AttentionConfig, self.HandleConfig, namespace='/current/attention')

        # start timer
        self.timer = rospy.Timer(rospy.Duration.from_sec(1.0 / self.synthesizer_rate), self.HandleTimer)


    def UpdateStateDisplay(self):
        self.config_server.update_configuration({
            "eyecontact_state": self.eyecontact,
            "lookat_state": self.lookat,
            "mirroring_state": self.mirroring,
            "gaze_state": self.gaze,
        })


    def HandleConfig(self, config, level):
        with self.lock:

            if self.enable_flag != config.enable_flag:
                self.enable_flag = config.enable_flag
                # TODO: enable or disable the behaviors

            # keep time
            self.keep_time = config.keep_time
            self.attetntion_region = config.attention_region
            self.head_speed = config.head_speed
            # update the counter ranges (and counters if the ranges changed)
            if config.saliency_time_max < config.saliency_time_min:
                config.saliency_time_max = config.saliency_time_min
            if config.saliency_time_min != self.saliency_time_min or config.saliency_time_max != self.saliency_time_max:
                self.saliency_time_min = config.saliency_time_min
                self.saliency_time_max = config.saliency_time_max
                self.InitSaliencyCounter()

            if config.faces_time_max < config.faces_time_min:
                config.faces_time_max = config.faces_time_min
            if config.faces_time_min != self.faces_time_min or config.faces_time_max != self.faces_time_max:
                self.faces_time_min = config.faces_time_min
                self.faces_time_max = config.faces_time_max
                self.InitFacesCounter()

            if config.eyes_time_max < config.eyes_time_min:
                config.eyes_time_max = config.eyes_time_min
            if config.eyes_time_min != self.eyes_time_min or config.eyes_time_max != self.eyes_time_max:
                self.eyes_time_min = config.eyes_time_min
                self.eyes_time_max = config.eyes_time_max
                self.InitEyesCounter()

            if config.region_time_max < config.region_time_min:
                config.region_time_max = config.region_time_min
            if config.region_time_min != self.region_time_min or config.region_time_max != self.region_time_max:
                self.region_time_min = config.region_time_min
                self.region_time_max = config.region_time_max
                self.InitRegionCounter()

            self.face_state_decay = config.face_state_decay

            self.gaze_delay = config.gaze_delay
            self.gaze_speed = config.gaze_speed

            self.interrupt_to_all_faces = config.interrupt_to_all_faces
            self.interrupting = False

            if config.all_faces_start_time_max < config.all_faces_start_time_min:
                config.all_faces_start_time_max = config.all_faces_start_time_min
            if config.all_faces_start_time_min != self.all_faces_start_time_min or config.all_faces_start_time_max != self.all_faces_start_time_max:
                self.all_faces_start_time_min = config.all_faces_start_time_min
                self.all_faces_start_time_max = config.all_faces_start_time_max
                self.InitAllFacesStartCounter()

            if config.all_faces_duration_max < config.all_faces_duration_min:
                config.all_faces_duration_max = config.all_faces_duration_min
            if config.all_faces_duration_min != self.all_faces_duration_min or config.all_faces_duration_max != self.all_faces_duration_max:
                self.all_faces_duration_min = config.all_faces_duration_min
                self.all_faces_duration_max = config.all_faces_duration_max
                self.InitAllFacesDurationCounter()

            # and set the states for each state machine
            self.SetEyeContact(config.eyecontact_state)
            self.SetLookAt(config.lookat_state)
            self.SetMirroring(config.mirroring_state)
            self.SetGaze(config.gaze_state)

        if self.synthesizer_rate != config.synthesizer_rate and not self.configs_init:
            self.synthesizer_rate = config.synthesizer_rate
            self.timer.shutdown()
            self.timer = rospy.Timer(rospy.Duration.from_sec(1.0 / self.synthesizer_rate), self.HandleTimer)
            self.InitSaliencyCounter()
            self.InitFacesCounter()
            self.InitEyesCounter()
            self.InitRegionCounter()
            self.InitAllFacesStartCounter()
            self.InitAllFacesDurationCounter()

        self.configs_init = True
        return config


    def getBlenderPos(self, pos, ts, frame_id):
        if frame_id == 'blender':
            return pos
        else:
            ps = PointStamped()
            ps.header.seq = 0
            ps.header.stamp = ts
            ps.header.frame_id = frame_id
            ps.point.x = pos.x
            ps.point.y = pos.y
            ps.point.z = pos.z
            if self.tf_listener.canTransform("blender", frame_id, ts):
                pst = self.tf_listener.transformPoint("blender", ps)
                return pst.point
            else:
                raise Exception("tf from robot to blender did not work")


    def SetGazeFocus(self, pos, speed, ts, frame_id='robot'):
        try:
            pos = self.getBlenderPos(pos, ts, frame_id)
            msg = Target()
            msg.x = pos.x
            msg.y = pos.y
            msg.z = pos.z
            msg.speed = speed
            self.gaze_focus_pub.publish(msg)
        except Exception as e:
            logger.warn("Gaze focus exception: {}".format(e))

    def SetHeadFocus(self, pos, speed, ts, frame_id='robot'):
        try:
            pos = self.getBlenderPos(pos, ts, frame_id)
            msg = Target()
            msg.x = pos.x
            msg.y = pos.y
            msg.z = pos.z
            msg.speed = speed
            self.head_focus_pub.publish(msg)
        except Exception as e:
            logger.warn("Head focus exception: {}".format(e))


    def UpdateGaze(self, pos, ts, frame_id="robot"):

        self.gaze_pos = pos

        if self.gaze == Gaze.GAZE_ONLY:
            self.SetGazeFocus(pos, 5.0, ts, frame_id)

        elif self.gaze == Gaze.HEAD_ONLY:
            self.SetHeadFocus(pos, self.head_speed, ts, frame_id)

        elif self.gaze == Gaze.GAZE_AND_HEAD:
            self.SetGazeFocus(pos, 5.0, ts, frame_id)
            self.SetHeadFocus(pos, self.head_speed, ts, frame_id)

        elif self.gaze == Gaze.GAZE_LEADS_HEAD:
            self.SetGazeFocus(pos, 5.0, ts, frame_id)

        elif self.gaze == Gaze.HEAD_LEADS_GAZE:
            self.SetHeadFocus(pos, self.head_speed, ts, frame_id)


    def SelectNextFace(self):

        # switch to the next (or first) face
        if self.state is None or len(self.state.faces) == 0:
            # there are no faces, so select none
            self.current_face_index = -1
            return


        if self.lookat  == LookAt.NEAREST_FACE:
            self.current_face_index, f = min(enumerate(self.state.faces),
                                             key=lambda f: (f[1].position.x**2+f[1].position.y**2))
        else:
            # Pick random or any other face from list
            if self.current_face_index == -1:
                self.current_face_index = 0
            else:
                self.current_face_index += 1
                if self.current_face_index >= len(self.state.faces):
                    self.current_face_index = 0


    def SelectNextSalientPoint(self):

        # switch to the next (or first) saliency vector
        if (self.state == None):
            self.current_saliency_index = -1
        if len(self.state.salientpoints) == 0:
            # there are no saliency vectors, so select none
            self.current_saliency_index = -1
            return
        if self.current_saliency_index == -1:
            self.current_saliency_index = 0
        else:
            self.current_saliency_index += 1
            if self.current_saliency_index >= len(self.state.salientpoints):
                self.current_saliency_index = 0


    def SelectNextRegion(self):
        # switch to next region point(according to audience ROI)
        try:
            # Check if performance has set regions
            regions = rospy.get_param("/{}/performance_regions".format(self.robot_name), {})
            if len(regions) == 0:
                regions = rospy.get_param("/{}/egions".format(self.robot_name), {})
            point = AttentionRegion.get_point_from_regions(regions, REGIONS[self.attetntion_region])
            return Point(x=point['x'], y=point['y'], z=point['z'])
        except Exception as e:
            logger.warn("Could not find new attention point: {}".format(e))


    def StepLookAtFace(self, ts):

        if self.current_face_index == -1:
            raise Exception("No face available")


        curface = self.state.faces[self.current_face_index]
        face_pos = curface.position

        # ==== handle eyecontact (only for LookAt.ONE_FACE and LookAt.ALL_FACES)

        # calculate where left eye, right eye and mouth are on the current face
        left_eye_pos = Point()
        right_eye_pos = Point()
        mouth_pos = Point()

        # all are 5cm in front of the center of the face
        left_eye_pos.x = face_pos.x - 0.05
        right_eye_pos.x = face_pos.x - 0.05
        mouth_pos.x = face_pos.x - 0.05

        left_eye_pos.y = face_pos.y + 0.03  # left eye is 3cm to the left of the center
        right_eye_pos.y = face_pos.y - 0.03  # right eye is 3cm to the right of the center
        mouth_pos.y = face_pos.y  # mouth is dead center

        left_eye_pos.z = face_pos.z + 0.06  # left eye is 6cm above the center
        right_eye_pos.z = face_pos.z + 0.06  # right eye is 6cm above the center
        mouth_pos.z = face_pos.z - 0.04  # mouth is 4cm below the center

        if self.eyecontact == EyeContact.IDLE:
            # look at center of the head
            self.UpdateGaze(face_pos, ts)

        elif self.eyecontact == EyeContact.LEFT_EYE:
            # look at left eye
            self.UpdateGaze(left_eye_pos, ts)

        elif self.eyecontact == EyeContact.RIGHT_EYE:
            # look at right eye
            self.UpdateGaze(right_eye_pos, ts)

        elif self.eyecontact == EyeContact.BOTH_EYES:
            # switch between eyes back and forth
            self.eyes_counter -= 1
            if self.eyes_counter == 0:
                self.InitEyesCounter()
                if self.current_eye == 1:
                    self.current_eye = 0
                else:
                    self.current_eye = 1
            # look at that eye
            if self.current_eye == 0:
                cur_eye_pos = left_eye_pos
            else:
                cur_eye_pos = right_eye_pos
            self.UpdateGaze(cur_eye_pos, ts)

        elif self.eyecontact == EyeContact.TRIANGLE:
            # cycle between eyes and mouth
            self.eyes_counter -= 1
            if self.eyes_counter == 0:
                self.InitEyesCounter()
                if self.current_eye == 2:
                    self.current_eye = 0
                else:
                    self.current_eye += 1
            # look at that eye
            if self.current_eye == 0:
                cur_eye_pos = left_eye_pos
            elif self.current_eye == 1:
                cur_eye_pos = right_eye_pos
            elif self.current_eye == 2:
                cur_eye_pos = mouth_pos
            self.UpdateGaze(cur_eye_pos, ts)

        # mirroring
        msg = pau()
        msg.m_coeffs = []
        msg.m_shapekeys = []

        # if self.mirroring == Mirroring.EYEBROWS or self.mirroring == Mirroring.EYES or self.mirroring == Mirroring.MOUTH_EYEBROWS or self.mirroring == Mirroring.ALL:
        #    # mirror eyebrows
        #    left_brow = curface.left_brow
        #    right_brow = curface.right_brow
        #    msg.m_coeffs.append("brow_outer_UP.L")
        #    msg.m_shapekeys.append(left_brow)
        #    msg.m_coeffs.append("brow_inner_UP.L")
        #    msg.m_shapekeys.append(left_brow * 0.8)
        #    msg.m_coeffs.append("brow_outer_DN.L")
        #    msg.m_shapekeys.append(1.0 - left_brow)
        #    msg.m_coeffs.append("brow_outer_up.R")
        #    msg.m_shapekeys.append(right_brow)
        #    msg.m_coeffs.append("brow_inner_UP.R")
        #    msg.m_shapekeys.append(right_brow * 0.8)
        #    msg.m_coeffs.append("brow_outer_DN.R")
        #    msg.m_shapekeys.append(1.0 - right_brow)

        # if self.mirroring == Mirroring.EYELIDS or self.mirroring == Mirroring.EYES or self.mirroring == Mirroring.MOUTH_EYELIDS or self.mirroring == Mirroring.ALL:
        #    # mirror eyelids
        #    eyes_closed = ((1.0 - curface.left_eyelid) + (1.0 - curface.right_eyelid)) / 2.0
        #    msg.m_coeffs.append("eye-blink.UP.R")
        #    msg.m_shapekeys.append(eyes_closed)
        #    msg.m_coeffs.append("eye-blink.UP.L")
        #    msg.m_shapekeys.append(eyes_closed)
        #    msg.m_coeffs.append("eye-blink.LO.R")
        #    msg.m_shapekeys.append(eyes_closed)
        #    msg.m_coeffs.append("eye-blink.LO.L")
        #    msg.m_shapekeys.append(eyes_closed)

        # if self.mirroring == Mirroring.MOUTH or self.mirroring == Mirroring.MOUTH_EYEBROWS or self.mirroring == Mirroring.MOUTH_EYELIDS:
        #    # mirror mouth
        #    mouth_open = curface.mouth_open
        #    msg.m_coeffs.append("lip-JAW.DN")
        #    msg.m_shapekeys.append(mouth_open)

        # if self.mirroring != Mirroring.IDLE:
        #    self.StartPauMode()
        #    self.setpau_pub.publish(msg)


    def HandleTimer(self, data):

        with self.lock:

            if not self.configs_init:
                return False
            if not self.enable_flag:
                return False

            # this is the heart of the synthesizer, here the lookat and eyecontact state machines take care of where the robot is looking, and random expressions and gestures are triggered to look more alive (like RealSense Tracker)
            ts = data.current_expected
            #If nowhere to look, straighten the head
            idle_point = Point(x = 1, y=0, z=0)
            # ==== handle lookat
            if self.lookat == LookAt.IDLE:
                # no specific target, let Blender do it's soma cycle thing
                # Reset to look ahead to prevent weird
                self.UpdateGaze(idle_point,ts, frame_id='blender')
                ()

            elif self.lookat == LookAt.AVOID:
                # TODO: find out where there is no saliency, hand or face
                # TODO: head_focus_pub
                ()

            elif self.lookat == LookAt.HOLD:
                # Do nothing
                ()

            elif self.lookat == LookAt.SALIENCY:
                self.saliency_counter -= 1
                if self.saliency_counter == 0:
                    self.SelectNextSalientPoint()
                    # Reset head position if nothing happens
                    if self.current_saliency_index == -1:
                        self.UpdateGaze(idle_point, ts, frame_id='blender')
                    else:
                        # Init counter only if any salient point found
                        self.InitSaliencyCounter()

                if self.current_saliency_index != -1:
                    cursaliency = self.state.salientpoints[self.current_saliency_index]
                    self.UpdateGaze(cursaliency.position, ts)

            elif self.lookat == LookAt.REGION:
                self.region_counter -= 1
                if self.region_counter == 0:
                    self.InitRegionCounter()
                    # SelectNextRegion returns idle point if no region set
                    point = self.SelectNextRegion()
                    # Attention points are calculated in blender frame
                    self.UpdateGaze(point, ts, frame_id='blender')

            else:
                if self.lookat == LookAt.ALL_FACES or self.lookat == LookAt.NEAREST_FACE:
                    self.faces_counter -= 1
                    if self.faces_counter == 0:
                        self.InitFacesCounter()
                        self.SelectNextFace()
                try:
                    self.StepLookAtFace(ts)
                except:
                    self.UpdateGaze(idle_point, ts, frame_id='blender')


            # have gaze or head follow head or gaze after a while
            if self.gaze_delay_counter > 0 and self.gaze_pos != None:

                self.gaze_delay_counter -= 1
                if self.gaze_delay_counter == 0:

                    if self.gaze == Gaze.GAZE_LEADS_HEAD:
                        self.SetHeadFocus(self.gaze_pos, self.gaze_speed,ts)
                        self.gaze_delay_counter = int(self.gaze_delay * self.synthesizer_rate)

                    elif self.gaze == Gaze.HEAD_LEADS_GAZE:
                        self.SetGazeFocus(self.gaze_pos, self.gaze_speed,ts)
                        self.gaze_delay_counter = int(self.gaze_delay * self.synthesizer_rate)

            # when speaking, sometimes look at all faces
            if self.interrupt_to_all_faces:

                if self.interrupting:
                    self.all_faces_duration_counter -= 1
                    if self.all_faces_duration_counter <= 0:
                        self.interrupting = False
                        self.InitAllFacesDurationCounter()
                        self.SetLookAt(self.interrupted_state)
                        self.UpdateStateDisplay()
                else:
                    self.all_faces_start_counter -= 1
                    if self.all_faces_start_counter <= 0:
                        self.interrupting = True
                        self.InitAllFacesStartCounter()
                        self.interrupted_state = self.lookat
                        self.SetLookAt(LookAt.ALL_FACES)
                        self.UpdateStateDisplay()


    def SetEyeContact(self, neweyecontact):

        logger.warn("SetEyeContact {}".format(neweyecontact))
        if neweyecontact == self.eyecontact:
            return

        self.eyecontact = neweyecontact

        if self.eyecontact == EyeContact.BOTH_EYES or self.eyecontact == EyeContact.TRIANGLE:
            self.InitEyesCounter()


    def SetLookAt(self, newlookat):

        logger.warn("SetLookAt {}".format(newlookat))
        if newlookat == self.lookat:
            return

        self.lookat = newlookat

        if self.lookat == LookAt.SALIENCY:
            self.InitSaliencyCounter()

        elif self.lookat == LookAt.ONE_FACE:
            self.InitEyesCounter()

        elif self.lookat == LookAt.ALL_FACES:
            self.InitFacesCounter()
            self.InitEyesCounter()

        elif self.lookat == LookAt.REGION:
            self.InitRegionCounter()


    def StartPauMode(self):

        mode = UInt8()
        mode.data = 148
        self.animationmode_pub.publish(mode)


    def StopPauMode(self):

        mode = UInt8()
        mode.data = 0
        self.animationmode_pub.publish(mode)


    def SetMirroring(self, newmirroring):

        logger.warn("SetMirroring {}".format(newmirroring))
        if newmirroring == self.mirroring:
            return

        self.mirroring = newmirroring

        if self.mirroring == Mirroring.IDLE:
            self.StopPauMode()
        else:
            self.StartPauMode()


    def SetGaze(self, newgaze):

        logger.warn("SetGaze {}".format(newgaze))
        if newgaze == self.gaze:
            return

        self.gaze = newgaze

        if self.gaze == Gaze.GAZE_LEADS_HEAD or self.gaze == Gaze.HEAD_LEADS_GAZE:
            self.gaze_delay_counter = int(self.gaze_delay * self.synthesizer_rate)


    def HandleState(self, data):

        with self.lock:

            self.state = data

            # if there is a wanted face, try to find it and use that
            if self.wanted_face_id != 0:
                index = 0
                self.current_face_index = -1
                for face in self.state.faces:
                    if face.id == self.wanted_face_id:
                        self.current_face_index = index
                        break
                    index += 1

            # otherwise, just make sure current_face_index is valid
            elif (self.current_face_index >= len(self.state.faces)) or (self.current_face_index == -1):
                self.SelectNextFace()
            # TODO: it's better to have the robot look at the same ID, regardless of which index in the stateface list

            # if there is no current saliency or the current saliency is out of range, select a new current saliency
            if (self.current_saliency_index >= len(self.state.salientpoints)) or (self.current_saliency_index == -1):
                self.SelectNextSaliency()


    def HandleEyeContact(self,data):

        with self.lock:

            self.SetEyeContact(data)

        self.UpdateStateDisplay()


    def HandleLookAt(self,data):

        with self.lock:

            if data.mode == LookAt.ONE_FACE: # if client wants to the robot to look at one specific face (even if the face temporarily disappears from the state)
                self.wanted_face_id = data.id
            elif data.mode == LookAt.REGION:
                self.attention_region = data.id
            else:
                self.wanted_face_id = 0
            self.SetLookAt(self,data.mode)

        self.UpdateStateDisplay()


    def HandleMirroring(self,data):

        with self.lock:

            self.SetMirroring(data)

        self.UpdateStateDisplay()


    def HandleGaze(self,data):

        with self.lock:

            self.SetGaze(data)

        self.UpdateStateDisplay()


if __name__ == "__main__":
    rospy.init_node('attention')
    node = Attention()
    rospy.spin()
