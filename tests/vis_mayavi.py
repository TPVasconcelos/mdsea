#!/usr/local/bin/python
# coding: utf-8
from mdsea.vis.myv import MayaviAnimation
from tests import mdsea as md

sm = md.SysManager.load(simid="_mdsea_testsimulation")

anim = MayaviAnimation(sm)