import numpy as np
import random
import datetime
from selfdrive.controls.lib.drive_helpers import get_steer_max
from common.numpy_fast import clip
from common.realtime import DT_CTRL
from cereal import log
from common.numpy_fast import interp
from selfdrive.config import Conversions as CV
import common.log as trace1

class LatControlLQR():
  def __init__(self, CP):
    self.trLQR = trace1.Loger("076_conan_LQR_ctrl")   
    self.scale = CP.lateralTuning.lqr.scale
    self.ki = CP.lateralTuning.lqr.ki

    self.A = np.array(CP.lateralTuning.lqr.a).reshape((2, 2))
    self.B = np.array(CP.lateralTuning.lqr.b).reshape((2, 1))
    self.C = np.array(CP.lateralTuning.lqr.c).reshape((1, 2))
    self.K = np.array(CP.lateralTuning.lqr.k).reshape((1, 2))
    self.L = np.array(CP.lateralTuning.lqr.l).reshape((2, 1))
    self.dc_gain = CP.lateralTuning.lqr.dcGain

    self.x_hat = np.array([[0], [0]])
    self.i_unwind_rate = 0.3 * DT_CTRL
    self.i_rate = 1.0 * DT_CTRL

    self.sat_count_rate = 1.0 * DT_CTRL
    self.sat_limit = CP.steerLimitTimer

    self.reset()

  def reset(self):
    self.i_lqr = 0.0
    self.output_steer = 0.0
    self.sat_count = 0.0

  def _check_saturation(self, control, check_saturation, limit):
    saturated = abs(control) == limit

    if saturated and check_saturation:
      self.sat_count += self.sat_count_rate
    else:
      self.sat_count -= self.sat_count_rate

    self.sat_count = clip(self.sat_count, 0.0, 1.0)

    return self.sat_count > self.sat_limit


  def atom_tune( self, v_ego_kph, sr_value, CP ):  # 조향각에 따른 변화.
    self.sr_KPH = CP.atomTuning.sRKPH
    self.sr_BPV = CP.atomTuning.sRBPV
    self.sR_lqr_kiV  = CP.atomTuning.sRlqrkiV
    self.sR_lqr_scaleV = CP.atomTuning.sRlqrscaleV

    self.ki = []
    self.scale = []

    nPos = 0
    for steerRatio in self.sr_BPV:  # steerRatio
      self.ki.append( interp( sr_value, steerRatio, self.sR_lqr_kiV[nPos] ) )
      self.scale.append( interp( sr_value, steerRatio, self.sR_lqr_scaleV[nPos] ) )
      nPos += 1
      if nPos > 10:
        break

    rt_ki = interp( v_ego_kph, self.sr_KPH, self.ki )
    rt_scale  = interp( v_ego_kph, self.sr_KPH, self.scale )
     
    return rt_ki, rt_scale

  def update(self, active, CS, CP, path_plan):
    lqr_log = log.ControlsState.LateralLQRState.new_message()

    steers_max = get_steer_max(CP, CS.vEgo)
    torque_scale = (0.45 + CS.vEgo / 60.0)**2  # Scale actuator model with speed
    #neokii
    torque_scale = min(torque_scale, 0.65) 

    steering_angle = CS.steeringAngle
    steeringTQ = CS.steeringTorque

    v_ego_kph = CS.vEgo * CV.MS_TO_KPH
    self.ki, self.scale = self.atom_tune( v_ego_kph, CS.steeringAngle, CP )

    # ###  설정값 최적화 분석을 위한 랜덤화 임시 코드
    #now = datetime.datetime.now() # current date and time
    #micro_S = int(now.microsecond)
    #if micro_S < 10000 : #1초에 한번만 랜덤변환
    #  self.ki = random.uniform(0.015, 0.025)    #self.ki - (self.ki*0.5), self.ki + (self.ki*0.5) )
    #  self.scale = random.uniform(1750, 1950)     #int(self.scale) - int(self.scale*0.055), int(self.scale) + int(self.scale*0.055) ) )
    #  self.dc_gain = random.uniform(0.0028, 0.0032)  #self.dc_gain - (self.dc_gain*0.1), self.dc_gain + (self.dc_gain*0.1) )    
    #  steers_max = random.uniform(1.0, 1.2)
    # ########################### 

    log_ki = self.ki
    log_scale = self.scale
    log_dc_gain = self.dc_gain   

    # Subtract offset. Zero angle should correspond to zero torque
    self.angle_steers_des = path_plan.angleSteers - path_plan.angleOffset
    steering_angle -= path_plan.angleOffset

    # Update Kalman filter
    angle_steers_k = float(self.C.dot(self.x_hat))
    e = steering_angle - angle_steers_k
    self.x_hat = self.A.dot(self.x_hat) + self.B.dot(CS.steeringTorqueEps / torque_scale) + self.L.dot(e)
    error = self.angle_steers_des - angle_steers_k
    u_lqr = float(self.angle_steers_des / self.dc_gain - self.K.dot(self.x_hat))

    if CS.vEgo < 0.3 or not active:
      lqr_log.active = False
      lqr_output = 0.
      self.reset()
    else:
      lqr_log.active = True
      # LQR
      #u_lqr = float(self.angle_steers_des / self.dc_gain - self.K.dot(self.x_hat))
      lqr_output = torque_scale * u_lqr / self.scale

      # Integrator
      if CS.steeringPressed:
        self.i_lqr -= self.i_unwind_rate * float(np.sign(self.i_lqr))
      else:
        #error = self.angle_steers_des - angle_steers_k
        i = self.i_lqr + self.ki * self.i_rate * error
        control = lqr_output + i

        if (error >= 0 and (control <= steers_max or i < 0.0)) or \
           (error <= 0 and (control >= -steers_max or i > 0.0)):
          self.i_lqr = i

      self.output_steer = lqr_output + self.i_lqr
      self.output_steer = clip(self.output_steer, -steers_max, steers_max)

    check_saturation = (CS.vEgo > 10) and not CS.steeringRateLimited and not CS.steeringPressed
    saturated = self._check_saturation(self.output_steer, check_saturation, steers_max)

    if not CS.steeringPressed:
      str2 = '/{} /{} /{} /{} /{} /{} /{} /{} /{} /{} /{} /{} /{} /{} /{} /{}'.format(   
              v_ego_kph, steering_angle, self.angle_steers_des, angle_steers_k, error, steeringTQ, torque_scale, log_scale, log_ki, log_dc_gain, u_lqr, lqr_output, self.i_lqr, steers_max, self.output_steer, saturated )
      self.trLQR.add( str2 )

    lqr_log.steerAngle = angle_steers_k + path_plan.angleOffset
    lqr_log.i = self.i_lqr
    lqr_log.output = self.output_steer
    lqr_log.lqrOutput = lqr_output
    lqr_log.saturated = saturated
    return self.output_steer, float(self.angle_steers_des), lqr_log

  def update_log(self):
    str5 = 'LQR 설정값 : scale={:06.1f} / dc_gain={:06.4f} / ki={:05.3f} / O_ST={:5.3f}'.format(self.scale, self.dc_gain, self.ki, self.output_steer )
    trace1.printf2( str5 )
