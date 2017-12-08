#!/usr/bin/env python
# -*- coding: latin-1; -*-
#
# FSICoupler.py
# Main file (Python core) of CUPyDO.
# Authors : David THOMAS, Marco Lucio CERQUAGLIA, Romain BOMAN
#
# COPYRIGHT (C) University of Liège, 2017.

# ----------------------------------------------------------------------
#  Imports
# ----------------------------------------------------------------------

from math import *
import numpy as np
import scipy as sp
from scipy import spatial
import scipy.sparse.linalg as splinalg
import os, os.path, sys, string
import time as tm

import traceback

import socket, fnmatch
import fsi_pyutils

import copy

import ccupydo

np.set_printoptions(threshold=np.nan)

# global vars (underscore prevent them to be imported with "from module import *")
_theModule  = None
_theWDir    = None # workspace directory
_theWDirRoot = os.getcwd()  # base directory du workspace

# ----------------------------------------------------------------------
#    Interpolator class
# ----------------------------------------------------------------------

class InterfaceInterpolator(ccupydo.CInterpolator):
    """
    Interpolator of CUPyDO.
    Perform inteporlation of fluid-structure meshes.
    Inherited public members :
        -matching_fillMatrix()
        -TPS_fillMatrixA()
        -TPS_fillMatrixB()
        -RBF_fillMatrixA()
        -RBF_fillMatrixB()
        -PHI_TPS()
        -PHI_RBF()
        -distance()
    """

    def __init__(self, Manager, FluidSolver, SolidSolver, mpiComm = None, chtTransferMethod=None, heatTransferCoeff=1.0):
        """
        Description.
        """

        mpiPrint('\n***************************** Initializing FSI interpolator *****************************', mpiComm)

        ccupydo.CInterpolator.__init__(self, Manager)

        self.manager = Manager
        self.SolidSolver = SolidSolver
        self.FluidSolver = FluidSolver

        self.mappingTimer = Timer()

        self.nf = self.manager.getNumberOfFluidInterfaceNodes()
        self.ns = self.manager.getNumberOfSolidInterfaceNodes()
        self.nf_loc = self.manager.getNumberOfLocalFluidInterfaceNodes()
        self.ns_loc = self.manager.getNumberOfLocalSolidInterfaceNodes()
        self.nDim = self.manager.getnDim()

        self.d = 0

        if self.manager.withCht:
            self.chtTransferMethod = chtTransferMethod
            if self.chtTransferMethod not in ['TFFB','FFTB','hFTB','hFFB']:
                mpiPrint('CHT transfer method not specified or not recognized, using default TFFB',mpiComm)
                self.chtTransferMethod = 'TFFB'
        else:
            self.chtTransferMethod = None

        if self.chtTransferMethod in ['hFTB','hFFB']:
            self.heatTransferCoeff = heatTransferCoeff
        else:
            self.heatTransferCoeff = None

        self.mpiComm = mpiComm

        if self.mpiComm != None:
            self.myid = self.mpiComm.Get_rank()
            self.mpiSize = self.mpiComm.Get_size()
        else:
            self.myid = 0
            self.mpiSize = 1

        self.solidInterfaceDisplacement = None
        self.fluidInterfaceDisplacement = None
        self.solidInterfaceLoads = None
        self.fluidInterfaceLoads = None

        self.solidInterfaceHeatFlux = None
        self.fluidInterfaceHeatFlux = None
        self.solidInterfaceTemperature = None
        self.fluidInterfaceTemperature = None
        self.fluidInterfaceNormalHeatFlux = None
        self.solidInterfaceNormalHeatFlux = None
        self.fluidInterfaceRobinTemperature = None
        self.solidInterfaceRobinTemperature = None

    def checkTotalLoad(self):
        """
        Des.
        """

        FX, FY, FZ = self.solidInterfaceLoads.sum()

        FFX, FFY, FFZ = self.fluidInterfaceLoads.sum()

        mpiPrint("Checking f/s interface total force...", self.mpiComm)
        mpiPrint('Solid side (Fx, Fy, Fz) = ({}, {}, {})'.format(FX, FY, FZ), self.mpiComm)
        mpiPrint('Fluid side (Fx, Fy, Fz) = ({}, {}, {})'.format(FFX, FFY, FFZ), self.mpiComm)

    def getDisplacementFromSolidSolver(self):
        """
        Des.
        """

        if self.myid in self.manager.getSolidInterfaceProcessors():
            localSolidInterfaceDisp_X, localSolidInterfaceDisp_Y, localSolidInterfaceDisp_Z = self.SolidSolver.getNodalDisplacements()
            for iVertex in range(self.ns_loc):
                iGlobalVertex = self.manager.getGlobalIndex('solid', self.myid, iVertex)
                self.solidInterfaceDisplacement[iGlobalVertex] = [localSolidInterfaceDisp_X[iVertex], localSolidInterfaceDisp_Y[iVertex], localSolidInterfaceDisp_Z[iVertex]]

        self.solidInterfaceDisplacement.assemble()

    def getHeatFluxFromSolidSolver(self):
        """
        Des.
        """

        if self.myid in self.manager.getSolidInterfaceProcessors():
            localSolidInterfaceHeatFlux_X, localSolidInterfaceHeatFlux_Y, localSolidInterfaceHeatFlux_Z = self.SolidSolver.getNodalHeatFluxes()
            for iVertex in range(self.ns_loc):
                iGlobalVertex = self.manager.getGlobalIndex('solid', self.myid, iVertex)
                self.solidInterfaceHeatFlux[iGlobalVertex] = [localSolidInterfaceHeatFlux_X[iVertex], localSolidInterfaceHeatFlux_Y[iVertex], localSolidInterfaceHeatFlux_Z[iVertex]]

        self.solidInterfaceHeatFlux.assemble()

    def getLoadsFromFluidSolver(self):
        """
        Des.
        """

        if self.myid in self.manager.getFluidInterfaceProcessors():
            localFluidInterfaceLoad_X, localFluidInterfaceLoad_Y, localFluidInterfaceLoad_Z = self.FluidSolver.getNodalLoads()
            for iVertex in range(self.nf_loc):
                iGlobalVertex = self.manager.getGlobalIndex('fluid', self.myid, iVertex)
                self.fluidInterfaceLoads[iGlobalVertex] = [localFluidInterfaceLoad_X[iVertex], localFluidInterfaceLoad_Y[iVertex], localFluidInterfaceLoad_Z[iVertex]]

        self.fluidInterfaceLoads.assemble()

    def getTemperatureFromFluidSolver(self):
        """
        Des.
        """

        if self.myid in self.manager.getFluidInterfaceProcessors():
            localFluidInterfaceTemperature = self.FluidSolver.getNodalTemperatures()
            for iVertex in range(self.nf_loc):
                iGlobalVertex = self.manager.getGlobalIndex('fluid', self.myid, iVertex)
                self.fluidInterfaceTemperature[iGlobalVertex] = [localFluidInterfaceTemperature[iVertex]]

        self.fluidInterfaceTemperature.assemble()

    def getRobinTemperatureFromFluidSolver(self):
        """
        Des.
        """

        if self.myid in self.manager.getFluidInterfaceProcessors():
            localFluidInterfaceNormalHeatFlux = self.FluidSolver.getNodalNormalHeatFlux()
            localFluidInterfaceTemperature = self.FluidSolver.getNodalTemperatures()
            localFluidInterfaceRobinTemperature = localFluidInterfaceTemperature - (localFluidInterfaceNormalHeatFlux/self.heatTransferCoeff)
            for iVertex in range(self.nf_loc):
                iGlobalVertex = self.manager.getGlobalIndex('fluid', self.myid, iVertex)
                self.fluidInterfaceRobinTemperature[iGlobalVertex] = [localFluidInterfaceRobinTemperature[iVertex]]

        self.fluidInterfaceRobinTemperature.assemble()

    def getHeatFluxFromFluidSolver(self):
        """
        Des.
        """

        if self.myid in self.manager.getFluidInterfaceProcessors():
            localFluidInterfaceHeatFlux_X, localFluidInterfaceHeatFlux_Y, localFluidInterfaceHeatFlux_Z = self.FluidSolver.getNodalHeatFluxes()
            localFluidInterfaceNormalHeatFlux = self.FluidSolver.getNodalNormalHeatFlux()
            for iVertex in range(self.nf_loc):
                iGlobalVertex = self.manager.getGlobalIndex('fluid', self.myid, iVertex)
                self.fluidInterfaceHeatFlux[iGlobalVertex] = [localFluidInterfaceHeatFlux_X[iVertex], localFluidInterfaceHeatFlux_Y[iVertex], localFluidInterfaceHeatFlux_Z[iVertex]]
                self.fluidInterfaceNormalHeatFlux[iGlobalVertex] = [localFluidInterfaceNormalHeatFlux[iVertex]]

        self.fluidInterfaceHeatFlux.assemble()
        self.fluidInterfaceNormalHeatFlux.assemble()

    def redistributeDataToFluidSolver(self, fluidInterfaceData):
        """
        Description
        """

        localFluidInterfaceData_array = None
        haloNodesData = {}

        if self.mpiComm != None:
            localSize = fluidInterfaceData.getDataArray(0).shape[0]
            fluidInterfaceData_array_recon = []
            for iDim in range(fluidInterfaceData.nDim):
                array_recon = mpiGatherv(fluidInterfaceData.getDataArray(iDim), localSize, self.nf, self.mpiComm, 0)
                fluidInterfaceData_array_recon.append(array_recon)
            haloNodesData = {}
            haloNodesData_bis = {}
            if self.myid == 0:
                for iProc in self.manager.getFluidInterfaceProcessors():
                    fluidPhysicalInterfaceNodesDistribution = self.manager.getFluidPhysicalInterfaceNodesDistribution()
                    fluidGlobalIndexRange = self.manager.getFluidGlobalIndexRange()
                    sendBuff = []
                    for iDim in range(fluidInterfaceData.nDim):
                        sendBuff_i = np.zeros(fluidPhysicalInterfaceNodesDistribution[iProc])
                        sendBuff.append(sendBuff_i)
                    globalIndex = fluidGlobalIndexRange[iProc][0]
                    sendBuffHalo = {}
                    for iVertex in range(fluidPhysicalInterfaceNodesDistribution[iProc]):
                        for iDim in range(fluidInterfaceData.nDim):
                            sendBuff[iDim][iVertex] = fluidInterfaceData_array_recon[iDim][globalIndex]
                        globalIndex += 1
                    fluidHaloNodesList = self.manager.getFluidHaloNodesList()
                    fluidIndexing = self.manager.getFluidIndexing()
                    for key in fluidHaloNodesList[iProc].keys():
                        globalIndex = fluidIndexing[key]
                        sendBuffHalo[key] = []
                        for iDim in range(fluidInterfaceData.nDim):
                            sendBuffHalo[key].append(fluidInterfaceData_array_recon[iDim][globalIndex])
                    iTagSend = 1
                    for iDim in range(fluidInterfaceData.nDim):
                        self.mpiComm.Send(sendBuff[iDim], dest=iProc, tag = iTagSend)
                        iTagSend += 1
                    #self.mpiComm.send(sendBuffHalo, dest = iProc, tag=iTagSend)
                    sendBuffHalo_key = np.array(sendBuffHalo.keys())
                    sendBuffHalo_values = np.empty((sendBuffHalo_key.size, 3),dtype=float)
                    for ii in range(sendBuffHalo_key.size):
                        sendBuffHalo_values[ii] = np.array(sendBuffHalo[sendBuffHalo_key[ii]])
                    self.mpiComm.Send(np.array(sendBuffHalo_key.size), dest=iProc, tag=101)
                    self.mpiComm.Send(sendBuffHalo_key, dest=iProc, tag=102)
                    self.mpiComm.Send(sendBuffHalo_values, dest=iProc, tag=103)
            if self.myid in self.manager.getFluidInterfaceProcessors():
                localFluidInterfaceData_array = []
                iTagRec = 1
                for iDim in range(fluidInterfaceData.nDim):
                    local_array = np.zeros(self.nf_loc)
                    self.mpiComm.Recv(local_array, source=0, tag=iTagRec)
                    localFluidInterfaceData_array.append(local_array)
                    iTagRec += 1
                #haloNodesData = self.mpiComm.recv(source=0, tag=iTagRec)
                nHaloNodesRcv = np.empty(1, dtype=int)
                self.mpiComm.Recv(nHaloNodesRcv, source=0, tag=101)
                rcvBuffHalo_keyBuff = np.empty(nHaloNodesRcv[0], dtype=int)
                self.mpiComm.Recv(rcvBuffHalo_keyBuff, source=0, tag=102)
                rcvBuffHalo_values = np.empty((nHaloNodesRcv[0],3), dtype=float)
                self.mpiComm.Recv(rcvBuffHalo_values, source=0, tag=103)
                for ii in range(len(rcvBuffHalo_keyBuff)):
                    haloNodesData_bis[rcvBuffHalo_keyBuff[ii]] = list(rcvBuffHalo_values[ii])
                haloNodesData = haloNodesData_bis


        return (localFluidInterfaceData_array, haloNodesData)

    def redistributeDataToSolidSolver(self, solidInterfaceData):
        """
        Des.
        """

        localSolidInterfaceData_array = None
        haloNodesData = {}
        haloNodesData_bis = {}

        if self.mpiComm != None:
            localSize = solidInterfaceData.getDataArray(0).shape[0]
            solidInterfaceData_array_recon = []
            for iDim in range(solidInterfaceData.nDim):
                array_recon = mpiGatherv(solidInterfaceData.getDataArray(iDim), localSize, self.ns+self.d, self.mpiComm, 0)
                solidInterfaceData_array_recon.append(array_recon)
            haloNodesData = {}
            if self.myid == 0:
                for iProc in self.manager.getSolidInterfaceProcessors():
                    solidPhysicalInterfaceNodesDistribution = self.manager.getSolidPhysicalInterfaceNodesDistribution()
                    solidGlobalIndexRange = self.manager.getSolidGlobalIndexRange()
                    sendBuff = []
                    for iDim in range(solidInterfaceData.nDim):
                        sendBuff_i = np.zeros(solidPhysicalInterfaceNodesDistribution[iProc])
                        sendBuff.append(sendBuff_i)
                    globalIndex = solidGlobalIndexRange[iProc][0]
                    sendBuffHalo = {}
                    for iVertex in range(solidPhysicalInterfaceNodesDistribution[iProc]):
                        for iDim in range(solidInterfaceData.nDim):
                            sendBuff[iDim][iVertex] = solidInterfaceData_array_recon[iDim][globalIndex]
                        globalIndex += 1
                    solidHaloNodesList = self.manager.getSolidHaloNodesList()
                    solidIndexing = self.manager.getSolidIndexing()
                    for key in solidHaloNodesList[iProc].keys():
                        globalIndex = solidIndexing[key]
                        sendBuffHalo[key] = []
                        for iDim in range(solidInterfaceData.nDim):
                            sendBuffHalo[key].append(solidInterfaceData_array_recon[iDim][globalIndex])
                    iTagSend = 1
                    for iDim in range(solidInterfaceData.nDim):
                        self.mpiComm.Send(sendBuff[iDim], dest=iProc, tag = iTagSend)
                        iTagSend += 1
                    #self.mpiComm.send(sendBuffHalo, dest = iProc, tag=iTagSend)
                    sendBuffHalo_key = np.array(sendBuffHalo.keys())
                    sendBuffHalo_values = np.empty((sendBuffHalo_key.size, 3),dtype=float)
                    for ii in range(sendBuffHalo_key.size):
                        sendBuffHalo_values[ii] = np.array(sendBuffHalo[sendBuffHalo_key[ii]])
                    self.mpiComm.Send(np.array(sendBuffHalo_key.size), dest=iProc, tag=101)
                    self.mpiComm.Send(sendBuffHalo_key, dest=iProc, tag=102)
                    self.mpiComm.Send(sendBuffHalo_values, dest=iProc, tag=103)
            if self.myid in self.manager.getSolidInterfaceProcessors():
                localSolidInterfaceData_array = []
                iTagRec = 1
                for iDim in range(solidInterfaceData.nDim):
                    local_array = np.zeros(self.ns_loc)
                    self.mpiComm.Recv(local_array, source=0, tag = iTagRec)
                    localSolidInterfaceData_array.append(local_array)
                    iTagRec += 1
                #haloNodesData = self.mpiComm.recv(source=0, tag=iTagRec)
                nHaloNodesRcv = np.empty(1, dtype=int)
                self.mpiComm.Recv(nHaloNodesRcv, source=0, tag=101)
                rcvBuffHalo_keyBuff = np.empty(nHaloNodesRcv[0], dtype=int)
                self.mpiComm.Recv(rcvBuffHalo_keyBuff, source=0, tag=102)
                rcvBuffHalo_values = np.empty((nHaloNodesRcv[0],3), dtype=float)
                self.mpiComm.Recv(rcvBuffHalo_values, source=0, tag=103)
                for ii in range(len(rcvBuffHalo_keyBuff)):
                    haloNodesData_bis[rcvBuffHalo_keyBuff[ii]] = list(rcvBuffHalo_values[ii])
                haloNodesData = haloNodesData_bis

        return (localSolidInterfaceData_array, haloNodesData)

    def setLoadsToSolidSolver(self, time):
        """
        des.
        """

        FFX, FFY, FFZ = self.fluidInterfaceLoads.sum()


        FX = 0.
        FY = 0.
        FZ = 0.

        FXT = 0.
        FYT = 0.
        FZT = 0.

        if self.mpiComm != None:
            (localSolidLoads_array, haloNodesSolidLoads) = self.redistributeDataToSolidSolver(self.solidInterfaceLoads)
            if self.myid in self.manager.getSolidInterfaceProcessors():
                self.SolidSolver.applyNodalLoads(localSolidLoads_array[0], localSolidLoads_array[1], localSolidLoads_array[2], time)
                FX = localSolidLoads_array[0].sum()
                FY = localSolidLoads_array[1].sum()
                FZ = localSolidLoads_array[2].sum()
            FXT = mpiAllReduce(self.mpiComm, FX)
            FYT = mpiAllReduce(self.mpiComm, FY)
            FZT = mpiAllReduce(self.mpiComm, FZ)
        else:
            self.SolidSolver.applyNodalLoads(self.solidInterfaceLoads.getDataArray(0), self.solidInterfaceLoads.getDataArray(1), self.solidInterfaceLoads.getDataArray(2), time)
            FXT, FYT, FZT = self.solidInterfaceLoads.sum()

        mpiPrint("Checking f/s interface total force...", self.mpiComm)
        mpiPrint('Solid side (Fx, Fy, Fz) = ({}, {}, {})'.format(FXT, FYT, FZT), self.mpiComm)
        mpiPrint('Fluid side (Fx, Fy, Fz) = ({}, {}, {})'.format(FFX, FFY, FFZ), self.mpiComm)

    def setDisplacementToFluidSolver(self, time):
        """
        Des.
        """

        self.checkConservation()

        if self.mpiComm != None:
            (localFluidInterfaceDisplacement, haloNodesDisplacements) = self.redistributeDataToFluidSolver(self.fluidInterfaceDisplacement)
            if self.myid in self.manager.getFluidInterfaceProcessors():
                self.FluidSolver.applyNodalDisplacements(localFluidInterfaceDisplacement[0], localFluidInterfaceDisplacement[1], localFluidInterfaceDisplacement[2], localFluidInterfaceDisplacement[0], localFluidInterfaceDisplacement[1], localFluidInterfaceDisplacement[2], haloNodesDisplacements, time)
        else:
            self.FluidSolver.applyNodalDisplacements(self.fluidInterfaceDisplacement.getDataArray(0), self.fluidInterfaceDisplacement.getDataArray(1), self.fluidInterfaceDisplacement.getDataArray(2), self.fluidInterfaceDisplacement.getDataArray(0), self.fluidInterfaceDisplacement.getDataArray(1), self.fluidInterfaceDisplacement.getDataArray(2), {}, time)

    def setHeatFluxToFluidSolver(self, time):
        """
        Description.
        """

        if self.mpiComm != None:
            (localFluidInterfaceHeatFlux, haloNodesHeatFlux) = self.redistributeDataToFluidSolver(self.fluidInterfaceHeatFlux)
            if self.myid in self.manager.getFluidInterfaceProcessors():
                self.FluidSolver.applyNodalHeatFluxes(localFluidInterfaceHeatFlux[0], localFluidInterfaceHeatFlux[1], localFluidInterfaceHeatFlux[2], time)
        else:
            self.FluidSolver.applyNodalHeatFluxes(self.fluidInterfaceHeatFlux.getDataArray(0), self.fluidInterfaceHeatFlux.getDataArray(1), self.fluidInterfaceHeatFlux.getDataArray(2), time)

    def setTemperatureToFluidSolver(self, time):
        """
        Des.
        """

        if self.mpiComm != None:
            (localFluidInterfaceTemperature, haloNodesTemperature) = self.redistributeDataToFluidSolver(self.fluidInterfaceTemperature)
            if self.myid in self.manager.getFluidInterfaceProcessors():
                self.FluidSolver.applyNodalTemperatures(localFluidInterfaceTemperature[0], time)
        else:
            self.FluidSolver.applyNodalTemperatures(self.fluidInterfaceTemperature.getDataArray(0), time)

    def setTemperatureToSolidSolver(self, time):
        """
        Description
        """

        if self.mpiComm != None:
            (localSolidInterfaceTemperature, haloNodesTemperature) = self.redistributeDataToSolidSolver(self.solidInterfaceTemperature)
            if self.myid in self.manager.getSolidInterfaceProcessors():
                self.SolidSolver.applyNodalTemperatures(localSolidInterfaceTemperature[0], time)
        else:
            self.SolidSolver.applyNodalTemperatures(self.solidInterfaceTemperature.getDataArray(0), time)

    def setRobinHeatFluxToSolidSolver(self, time):
        """
        Def
        """

        if self.mpiComm != None:
            (localSolidInterfaceRobinTemperature, haloNodesRobinTemperature) = self.redistributeDataToSolidSolver(self.solidInterfaceRobinTemperature)
            if self.myid in self.manager.getSolidInterfaceProcessors():
                localSolidInterfaceTemperature = self.SolidSolver.getNodalTemperatures()
                localSolidInterfaceRobinHeatFlux = self.heatTransferCoeff*(localSolidInterfaceTemperature-localSolidInterfaceRobinTemperature[0])
                self.SolidSolver.applyNodalNormalHeatFluxes(localSolidInterfaceRobinHeatFlux, time)
        else:
            localSolidInterfaceTemperature = self.SolidSolver.getNodalTemperatures()
            localSolidInterfaceRobinHeatFlux = self.heatTransferCoeff*(localSolidInterfaceTemperature-self.solidInterfaceRobinTemperature.getDataArray(0), time)
            self.SolidSolver.applyNodalNormalHeatFluxes(localSolidInterfaceRobinHeatFlux, time)

    def setHeatFluxToSolidSolver(self, time):
        """
        Des.
        """

        if self.mpiComm != None:
            (localSolidInterfaceNormalHeatFlux, haloNodesNormalHeatFlux) =  self.redistributeDataToSolidSolver(self.solidInterfaceNormalHeatFlux)
            if self.myid in self.manager.getSolidInterfaceProcessors():
                self.SolidSolver.applyNodalNormalHeatFluxes(localSolidInterfaceNormalHeatFlux[0], time)
        else:
            self.SolidSolver.applyNodalNormalHeatFluxes(self.solidInterfaceNormalHeatFlux.getDataArray(0), time)

    def interpolateFluidLoadsOnSolidMesh(self):
        """
        Description
        """

        self.interpolateFluidToSolid(self.fluidInterfaceLoads, self.solidInterfaceLoads)

    def interpolateSolidDisplacementOnFluidMesh(self):
        """
        Description.
        """

        self.interpolateSolidToFluid(self.solidInterfaceDisplacement, self.fluidInterfaceDisplacement)

    def interpolateSolidHeatFluxOnFluidMesh(self):
        """
        Description.
        """

        self.interpolateSolidToFluid(self.solidInterfaceHeatFlux, self.fluidInterfaceHeatFlux)


    def interpolateSolidTemperatureOnFluidMesh(self):
        """
        Description
        """

        self.interpolateSolidToFluid(self.solidInterfaceTemperature, self.fluidInterfaceTemperature)

    def interpolateFluidHeatFluxOnSolidMesh(self):
        """
        Description.
        """

        self.interpolateFluidToSolid(self.fluidInterfaceHeatFlux, self.solidInterfaceHeatFlux)
        self.interpolateFluidToSolid(self.fluidInterfaceNormalHeatFlux, self.solidInterfaceNormalHeatFlux)

    def interpolateFluidTemperatureOnSolidMesh(self):
        """
        Description.
        """

        self.interpolateFluidToSolid(self.fluidInterfaceTemperature, self.solidInterfaceTemperature)

    def interpolateFluidRobinTemperatureOnSolidMesh(self):
        """
        Des.
        """

        self.interpolateFluidToSolid(self.fluidInterfaceRobinTemperature, self.solidInterfaceRobinTemperature)

    def getNs(self):
        """
        Des.
        """

        return self.ns

    def getNf(self):
        """
        Des.
        """

        return self.nf

    def getd(self):
        """
        Des.
        """

        return self.d

class MatchingMeshesInterpolator(InterfaceInterpolator):
    """
    Description.
    """

    def __init__(self, Manager, FluidSolver, SolidSolver, mpiComm = None, chtTransferMethod=None, heatTransferCoeff=1.0):
        """
        Description
        """

        InterfaceInterpolator.__init__(self, Manager, FluidSolver, SolidSolver, mpiComm, chtTransferMethod, heatTransferCoeff)

        mpiPrint('\nSetting matching meshes interpolator...', mpiComm)

        if self.nf != self.ns:
            raise Exception("Fluid and solid interface must have the same number of nodes for matching meshes ! ")
        ccupydo.CInterpolator.matching_initSearch(self)

        self.generateInterfaceData()

        self.generateMapping()

    def checkConservation(self):
        """
        Des.
        """

        WSX, WSY, WSZ = self.solidInterfaceLoads.dot(self.solidInterfaceDisplacement)

        WFX, WFY, WFZ = self.fluidInterfaceLoads.dot(self.fluidInterfaceDisplacement)

        mpiPrint("Checking f/s interface conservation...", self.mpiComm)
        mpiPrint('Solid side (Wx, Wy, Wz) = ({}, {}, {})'.format(WSX, WSY, WSZ), self.mpiComm)
        mpiPrint('Fluid side (Wx, Wy, Wz) = ({}, {}, {})'.format(WFX, WFY, WFZ), self.mpiComm)

    def generateInterfaceData(self):
        """
        Des.
        """

        if self.manager.withFsi:
            self.solidInterfaceDisplacement = FlexInterfaceData(self.ns, 3, self.mpiComm)
            self.fluidInterfaceDisplacement = FlexInterfaceData(self.nf, 3, self.mpiComm)
            self.solidInterfaceLoads = FlexInterfaceData(self.ns, 3, self.mpiComm)
            self.fluidInterfaceLoads = FlexInterfaceData(self.nf, 3, self.mpiComm)

        if self.manager.withCht :
            if self.chtTransferMethod == 'TFFB':
                self.solidInterfaceTemperature = FlexInterfaceData(self.ns, 1, self.mpiComm)
                self.fluidInterfaceTemperature = FlexInterfaceData(self.nf, 1, self.mpiComm)
                self.solidInterfaceHeatFlux = FlexInterfaceData(self.ns, 3, self.mpiComm)
                self.fluidInterfaceHeatFlux = FlexInterfaceData(self.nf, 3, self.mpiComm)
            elif self.chtTransferMethod == 'FFTB':
                self.solidInterfaceTemperature = FlexInterfaceData(self.ns, 1, self.mpiComm)
                self.fluidInterfaceTemperature = FlexInterfaceData(self.nf, 1, self.mpiComm)
                self.solidInterfaceHeatFlux = FlexInterfaceData(self.ns, 3, self.mpiComm)
                self.fluidInterfaceHeatFlux = FlexInterfaceData(self.nf, 3, self.mpiComm)
                self.fluidInterfaceNormalHeatFlux = FlexInterfaceData(self.nf, 1, self.mpiComm)
                self.solidInterfaceNormalHeatFlux = FlexInterfaceData(self.ns, 1, self.mpiComm)
            elif self.chtTransferMethod == 'hFTB':
                self.fluidInterfaceRobinTemperature = FlexInterfaceData(self.nf, 1, self.mpiComm)
                self.solidInterfaceRobinTemperature = FlexInterfaceData(self.ns, 1, self.mpiComm)
                self.solidInterfaceTemperature = FlexInterfaceData(self.ns, 1, self.mpiComm)
                self.fluidInterfaceTemperature = FlexInterfaceData(self.nf, 1, self.mpiComm)
            elif self.chtTransferMethod == 'hFFB':
                self.fluidInterfaceRobinTemperature = FlexInterfaceData(self.nf, 1, self.mpiComm)
                self.solidInterfaceRobinTemperature = FlexInterfaceData(self.ns, 1, self.mpiComm)
                self.solidInterfaceHeatFlux = FlexInterfaceData(self.ns, 3, self.mpiComm)
                self.fluidInterfaceHeatFlux = FlexInterfaceData(self.nf, 3, self.mpiComm)

        self.H = InterfaceMatrix((self.nf,self.ns), self.mpiComm)
        self.H_T = InterfaceMatrix((self.ns,self.nf), self.mpiComm)
        self.H.createSparse(1,1)
        self.H_T.createSparse(1,1)

    def generateMapping(self):
        """
        Des.
        """

        solidInterfaceProcessors = self.manager.getSolidInterfaceProcessors()
        fluidInterfaceProcessors = self.manager.getFluidInterfaceProcessors()
        solidPhysicalInterfaceNodesDistribution = self.manager.getSolidPhysicalInterfaceNodesDistribution()

        mpiPrint('\nBuilding interpolation matrix...', self.mpiComm)
        mpiPrint('\nBuilding matrix H of size {} X {}...'.format(self.nf, self.ns), self.mpiComm)
        self.mappingTimer.start()

        if self.mpiComm != None:
            for iProc in solidInterfaceProcessors:
                if self.myid == iProc:
                    localSolidInterface_array_X, localSolidInterface_array_Y, localSolidInterface_array_Z = self.SolidSolver.getNodalInitialPositions()
                    for jProc in fluidInterfaceProcessors:
                        self.mpiComm.Send(localSolidInterface_array_X, dest=jProc, tag=1)
                        self.mpiComm.Send(localSolidInterface_array_Y, dest=jProc, tag=2)
                        self.mpiComm.Send(localSolidInterface_array_Z, dest=jProc, tag=3)
                if self.myid in fluidInterfaceProcessors:
                    sizeOfBuff = solidPhysicalInterfaceNodesDistribution[iProc]
                    solidInterfaceBuffRcv_X = np.zeros(sizeOfBuff)
                    solidInterfaceBuffRcv_Y = np.zeros(sizeOfBuff)
                    solidInterfaceBuffRcv_Z = np.zeros(sizeOfBuff)
                    self.mpiComm.Recv(solidInterfaceBuffRcv_X, iProc, tag=1)
                    self.mpiComm.Recv(solidInterfaceBuffRcv_Y, iProc, tag=2)
                    self.mpiComm.Recv(solidInterfaceBuffRcv_Z, iProc, tag=3)
                    self.mappingSearch(solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, iProc)
            if self.myid in fluidInterfaceProcessors:
                self.fillMatrix()
        else:
            localSolidInterface_array_X, localSolidInterface_array_Y, localSolidInterface_array_Z = self.SolidSolver.getNodalInitialPositions()
            self.mappingSearch(localSolidInterface_array_X, localSolidInterface_array_Y, localSolidInterface_array_Z, 0)
            self.fillMatrix()

        mpiBarrier(self.mpiComm)
        mpiPrint("\nAssembling H & H_T...", self.mpiComm)
        start = tm.time()
        self.H.assemble()
        mpiBarrier(self.mpiComm)
        self.H_T.assemble()
        mpiBarrier(self.mpiComm)
        stop = tm.time()
        mpiPrint('Assembly performed in {} s'.format(stop-start), self.mpiComm)
        mpiPrint('Matrix H is built.', self.mpiComm)

        self.mappingTimer.stop()
        self.mappingTimer.cumul()

    def mappingSearch(self, solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, iProc):
        """
        Des.
        """

        localFluidInterface_array_X_init, localFluidInterface_array_Y_init, localFluidInterface_array_Z_init = self.FluidSolver.getNodalInitialPositions()

        print('Mathing mapping search on rank {}...'.format(self.myid))
        start = tm.time()
        ccupydo.CInterpolator.matching_search(self, localFluidInterface_array_X_init, localFluidInterface_array_Y_init, localFluidInterface_array_Z_init,
                                              solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, iProc)
        stop = tm.time()
        print('Search on rank {} in {} s'.format(self.myid,stop-start))

    def fillMatrix(self):
        """
        Des.
        """

        print('Building H on rank {}...'.format(self.myid))
        start = tm.time()
        ccupydo.CInterpolator.matching_fillMatrix(self, self.H, self.H_T)
        stop = tm.time()
        print('Built H on rank {} in {} s'.format(self.myid,stop-start))

    def interpolateFluidToSolid(self, fluidInterfaceData, solidInterfaceData):
        """
        des.
        """

        self.H_T.mult(fluidInterfaceData, solidInterfaceData)

    def interpolateSolidToFluid(self, solidInterfaceData, fluidInterfaceData):
        """
        Des.
        """

        self.H.mult(solidInterfaceData, fluidInterfaceData)

class ConservativeInterpolator(InterfaceInterpolator):
    """
    Description.
    """

    def __init__(self, Manager, FluidSolver, SolidSolver, mpiComm = None, chtTransferMethod=None, heatTransferCoeff=1.0):
        """
        Des.
        """

        InterfaceInterpolator.__init__(self, Manager, FluidSolver, SolidSolver, mpiComm, chtTransferMethod, heatTransferCoeff)

        mpiPrint('\nSetting non-matching conservative interpolator...', mpiComm)

        self.d = self.nDim+1
        self.SolverA = None
        self.SolverA_T = None

    def getLinearSolvers(self):
        """
        Des.
        """

        return [self.SolverA, self.SolverA_T]

    def checkConservation(self):
        """
        Des.
        """

        WSX, WSY, WSZ = self.solidInterfaceLoads.dot(self.solidInterfaceDisplacement)

        WFX, WFY, WFZ = self.fluidInterfaceLoads.dot(self.fluidInterfaceDisplacement)

        mpiPrint("Checking f/s interface conservation...", self.mpiComm)
        mpiPrint('Solid side (Wx, Wy, Wz) = ({}, {}, {})'.format(WSX, WSY, WSZ), self.mpiComm)
        mpiPrint('Fluid side (Wx, Wy, Wz) = ({}, {}, {})'.format(WFX, WFY, WFZ), self.mpiComm)

    def generateInterfaceData(self):
        """
        Description.
        """

        if self.manager.withFsi:
            self.solidInterfaceDisplacement = FlexInterfaceData(self.ns + self.d, 3, self.mpiComm)
            self.fluidInterfaceDisplacement = FlexInterfaceData(self.nf, 3, self.mpiComm)
            self.solidInterfaceLoads = FlexInterfaceData(self.ns + self.d, 3, self.mpiComm)
            self.fluidInterfaceLoads = FlexInterfaceData(self.nf, 3, self.mpiComm)

        if self.manager.withCht :
            if self.chtTransferMethod == 'TFFB':
                self.solidInterfaceTemperature = FlexInterfaceData(self.ns + self.d, 1, self.mpiComm)
                self.fluidInterfaceTemperature = FlexInterfaceData(self.nf, 1, self.mpiComm)
                self.solidInterfaceHeatFlux = FlexInterfaceData(self.ns + self.d, 3, self.mpiComm)
                self.fluidInterfaceHeatFlux = FlexInterfaceData(self.nf, 3, self.mpiComm)
            elif self.chtTransferMethod == 'FFTB':
                self.solidInterfaceTemperature = FlexInterfaceData(self.ns + self.d, 1, self.mpiComm)
                self.fluidInterfaceTemperature = FlexInterfaceData(self.nf, 1, self.mpiComm)
                self.solidInterfaceHeatFlux = FlexInterfaceData(self.ns + self.d, 3, self.mpiComm)
                self.fluidInterfaceHeatFlux = FlexInterfaceData(self.nf, 3, self.mpiComm)
                self.fluidInterfaceNormalHeatFlux = FlexInterfaceData(self.nf, 1, self.mpiComm)
                self.solidInterfaceNormalHeatFlux = FlexInterfaceData(self.ns, 1, self.mpiComm)
            elif self.chtTransferMethod == 'hFTB':
                self.fluidInterfaceRobinTemperature = FlexInterfaceData(self.nf, 1, self.mpiComm)
                self.solidInterfaceRobinTemperature = FlexInterfaceData(self.ns, 1, self.mpiComm)
                self.solidInterfaceTemperature = FlexInterfaceData(self.ns + self.d, 1, self.mpiComm)
                self.fluidInterfaceTemperature = FlexInterfaceData(self.nf, 1, self.mpiComm)
            elif self.chtTransferMethod == 'hFFB':
                self.fluidInterfaceRobinTemperature = FlexInterfaceData(self.nf, 1, self.mpiComm)
                self.solidInterfaceRobinTemperature = FlexInterfaceData(self.ns, 1, self.mpiComm)
                self.solidInterfaceHeatFlux = FlexInterfaceData(self.ns + self.d, 3, self.mpiComm)
                self.fluidInterfaceHeatFlux = FlexInterfaceData(self.nf, 3, self.mpiComm)

        self.A = InterfaceMatrix((self.ns+self.d,self.ns+self.d), self.mpiComm)
        self.A_T = InterfaceMatrix((self.ns+self.d,self.ns+self.d), self.mpiComm)
        self.B = InterfaceMatrix((self.nf,self.ns+self.d), self.mpiComm)
        self.B_T = InterfaceMatrix((self.ns+self.d,self.nf), self.mpiComm)

    def generateMapping(self):
        """
        Des.
        """

        solidInterfaceProcessors = self.manager.getSolidInterfaceProcessors()
        fluidInterfaceProcessors = self.manager.getFluidInterfaceProcessors()
        solidPhysicalInterfaceNodesDistribution = self.manager.getSolidPhysicalInterfaceNodesDistribution()

        mpiPrint('\nBuilding interpolation matrices...', self.mpiComm)

        mpiPrint('\nBuilding matrix A of size {} X {}...'.format(self.ns, self.ns), self.mpiComm)
        # Fill the matrix A
        if self.mpiComm != None:
            for iProc in solidInterfaceProcessors:
                if self.myid == iProc:
                    localSolidInterface_array_X, localSolidInterface_array_Y, localSolidInterface_array_Z = self.SolidSolver.getNodalInitialPositions()
                    for jProc in solidInterfaceProcessors:
                        self.mpiComm.Send(localSolidInterface_array_X, dest=jProc, tag=1)
                        self.mpiComm.Send(localSolidInterface_array_Y, dest=jProc, tag=2)
                        self.mpiComm.Send(localSolidInterface_array_Z, dest=jProc, tag=3)
                if self.myid in solidInterfaceProcessors:
                    sizeOfBuff = solidPhysicalInterfaceNodesDistribution[iProc]
                    solidInterfaceBuffRcv_X = np.zeros(sizeOfBuff)
                    solidInterfaceBuffRcv_Y = np.zeros(sizeOfBuff)
                    solidInterfaceBuffRcv_Z = np.zeros(sizeOfBuff)
                    self.mpiComm.Recv(solidInterfaceBuffRcv_X, iProc, tag=1)
                    self.mpiComm.Recv(solidInterfaceBuffRcv_Y, iProc, tag=2)
                    self.mpiComm.Recv(solidInterfaceBuffRcv_Z, iProc, tag=3)
                    self.fillMatrixA(solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, iProc)
        else:
            localSolidInterface_array_X, localSolidInterface_array_Y, localSolidInterface_array_Z = self.SolidSolver.getNodalInitialPositions()
            self.fillMatrixA(localSolidInterface_array_X, localSolidInterface_array_Y, localSolidInterface_array_Z, 0)

        mpiBarrier(self.mpiComm)
        mpiPrint("\nAssembling A & A_T...", self.mpiComm)
        start = tm.time()
        self.A.assemble()
        mpiBarrier(self.mpiComm)
        self.A_T.assemble()
        mpiBarrier(self.mpiComm)
        stop = tm.time()
        mpiPrint('Assembly performed in {} s'.format(stop-start), self.mpiComm)
        mpiPrint('Matrix A is built.', self.mpiComm)

        mpiPrint('\nBuilding matrix B of size {} X {}...'.format(self.nf, self.ns), self.mpiComm)
        # Fill the matrix B
        if self.mpiComm != None:
            for iProc in solidInterfaceProcessors:
                if self.myid == iProc:
                    for jProc in fluidInterfaceProcessors:
                        self.mpiComm.Send(localSolidInterface_array_X, dest=jProc, tag=1)
                        self.mpiComm.Send(localSolidInterface_array_Y, dest=jProc, tag=2)
                        self.mpiComm.Send(localSolidInterface_array_Z, dest=jProc, tag=3)
                if self.myid in fluidInterfaceProcessors:
                    sizeOfBuff = solidPhysicalInterfaceNodesDistribution[iProc]
                    solidInterfaceBuffRcv_X = np.zeros(sizeOfBuff)
                    solidInterfaceBuffRcv_Y = np.zeros(sizeOfBuff)
                    solidInterfaceBuffRcv_Z = np.zeros(sizeOfBuff)
                    self.mpiComm.Recv(solidInterfaceBuffRcv_X, iProc, tag=1)
                    self.mpiComm.Recv(solidInterfaceBuffRcv_Y, iProc, tag=2)
                    self.mpiComm.Recv(solidInterfaceBuffRcv_Z, iProc, tag=3)
                    self.fillMatrixB(solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, iProc)
        else:
            self.fillMatrixB(localSolidInterface_array_X, localSolidInterface_array_Y, localSolidInterface_array_Z, 0)

        mpiBarrier(self.mpiComm)
        mpiPrint("\nAssembling B & B_T...", self.mpiComm)
        start = tm.time()
        self.B.assemble()
        mpiBarrier(self.mpiComm)
        self.B_T.assemble()
        mpiBarrier(self.mpiComm)
        stop = tm.time()
        mpiPrint('Assembly performed in {} s'.format(stop-start), self.mpiComm)
        mpiPrint('Matrix B is built.', self.mpiComm)

        self.SolverA = LinearSolver(self.A, self.mpiComm)
        self.SolverA_T = LinearSolver(self.A_T, self.mpiComm)

    def interpolateFluidToSolid(self, fluidInterfaceData, solidInterfaceData):
        """
        des.
        """

        dim = fluidInterfaceData.getDim()
        gamma_array = FlexInterfaceData(self.ns + self.d, dim, self.mpiComm)

        self.B_T.mult(fluidInterfaceData, gamma_array)
        self.SolverA_T.solve(gamma_array, solidInterfaceData)

    def interpolateSolidToFluid(self, solidInterfaceData, fluidInterfaceData):
        """
        Des.
        """

        dim = solidInterfaceData.getDim()
        gamma_array = FlexInterfaceData(self.ns + self.d, dim, self.mpiComm)

        self.SolverA.solve(solidInterfaceData, gamma_array)
        self.B.mult(gamma_array, fluidInterfaceData)


class ConsistentInterpolator(InterfaceInterpolator):
    """
    Description.
    """

    def __init__(self, Manager, FluidSolver, SolidSolver, mpiComm = None, chtTransferMethod=None, heatTransferCoeff=1.0):
        """
        Des.
        """

        InterfaceInterpolator.__init__(self, Manager, FluidSolver, SolidSolver, mpiComm, chtTransferMethod, heatTransferCoeff)

        mpiPrint('\nSetting non-matching consistent interpolator...', mpiComm)

        self.d = self.nDim+1
        self.SolverA = None
        self.SolverC = None

    def getLinearSolvers(self):
        """
        Des.
        """

        return [self.SolverA, self.SolverC]

    def checkConservation(self):
        """
        Des.
        """

        mpiPrint('No conservation check for consistent interpolation.', self.mpiComm)

    def generateInterfaceData(self):
        """
        Description.
        """

        if self.manager.withFsi:
            self.solidInterfaceDisplacement = FlexInterfaceData(self.ns + self.d, 3, self.mpiComm)
            self.fluidInterfaceDisplacement = FlexInterfaceData(self.nf, 3, self.mpiComm)
            self.solidInterfaceLoads = FlexInterfaceData(self.ns, 3, self.mpiComm)
            self.fluidInterfaceLoads = FlexInterfaceData(self.nf + self.d, 3, self.mpiComm)

        if self.manager.withCht :
            if self.chtTransferMethod == 'TFFB':
                self.solidInterfaceTemperature = FlexInterfaceData(self.ns, 1, self.mpiComm)
                self.fluidInterfaceTemperature = FlexInterfaceData(self.nf + self.d, 1, self.mpiComm)
                self.solidInterfaceHeatFlux = FlexInterfaceData(self.ns + self.d, 3, self.mpiComm)
                self.fluidInterfaceHeatFlux = FlexInterfaceData(self.nf, 3, self.mpiComm)
            elif self.chtTransferMethod == 'FFTB':
                self.solidInterfaceTemperature = FlexInterfaceData(self.ns + self.d, 1, self.mpiComm)
                self.fluidInterfaceTemperature = FlexInterfaceData(self.nf, 1, self.mpiComm)
                self.solidInterfaceHeatFlux = FlexInterfaceData(self.ns, 3, self.mpiComm)
                self.fluidInterfaceHeatFlux = FlexInterfaceData(self.nf + self.d, 3, self.mpiComm)
                self.fluidInterfaceNormalHeatFlux = FlexInterfaceData(self.nf + self.d, 1, self.mpiComm)
                self.solidInterfaceNormalHeatFlux = FlexInterfaceData(self.ns, 1, self.mpiComm)
            elif self.chtTransferMethod == 'hFTB':
                self.fluidInterfaceRobinTemperature = FlexInterfaceData(self.nf + self.d, 1, self.mpiComm)
                self.solidInterfaceRobinTemperature = FlexInterfaceData(self.ns, 1, self.mpiComm)
                self.solidInterfaceTemperature = FlexInterfaceData(self.ns + self.d, 1, self.mpiComm)
                self.fluidInterfaceTemperature = FlexInterfaceData(self.nf, 1, self.mpiComm)
            elif self.chtTransferMethod == 'hFFB':
                self.fluidInterfaceRobinTemperature = FlexInterfaceData(self.nf + self.d, 1, self.mpiComm)
                self.solidInterfaceRobinTemperature = FlexInterfaceData(self.ns, 1, self.mpiComm)
                self.solidInterfaceHeatFlux = FlexInterfaceData(self.ns + self.d, 3, self.mpiComm)
                self.fluidInterfaceHeatFlux = FlexInterfaceData(self.nf, 3, self.mpiComm)

        self.A = InterfaceMatrix((self.ns+self.d,self.ns+self.d), self.mpiComm)
        self.B = InterfaceMatrix((self.nf,self.ns+self.d), self.mpiComm)
        self.C = InterfaceMatrix((self.nf+self.d,self.nf+self.d), self.mpiComm)
        self.D = InterfaceMatrix((self.ns,self.nf+self.d), self.mpiComm)

    def generateMapping(self):
        """
        Des.
        """

        solidInterfaceProcessors = self.manager.getSolidInterfaceProcessors()
        fluidInterfaceProcessors = self.manager.getFluidInterfaceProcessors()
        solidPhysicalInterfaceNodesDistribution = self.manager.getSolidPhysicalInterfaceNodesDistribution()
        fluidPhysicalInterfaceNodesDistribution = self.manager.getFluidPhysicalInterfaceNodesDistribution()

        mpiPrint('\nBuilding interpolation matrices...', self.mpiComm)

        mpiPrint('\nBuilding matrix A of size {} X {}...'.format(self.ns, self.ns), self.mpiComm)
        # Fill the matrix A
        if self.mpiComm != None:
            for iProc in solidInterfaceProcessors:
                if self.myid == iProc:
                    localSolidInterface_array_X, localSolidInterface_array_Y, localSolidInterface_array_Z = self.SolidSolver.getNodalInitialPositions()
                    for jProc in solidInterfaceProcessors:
                        self.mpiComm.Send(localSolidInterface_array_X, dest=jProc, tag=1)
                        self.mpiComm.Send(localSolidInterface_array_Y, dest=jProc, tag=2)
                        self.mpiComm.Send(localSolidInterface_array_Z, dest=jProc, tag=3)
                if self.myid in solidInterfaceProcessors:
                    sizeOfBuff = solidPhysicalInterfaceNodesDistribution[iProc]
                    solidInterfaceBuffRcv_X = np.zeros(sizeOfBuff)
                    solidInterfaceBuffRcv_Y = np.zeros(sizeOfBuff)
                    solidInterfaceBuffRcv_Z = np.zeros(sizeOfBuff)
                    self.mpiComm.Recv(solidInterfaceBuffRcv_X, iProc, tag=1)
                    self.mpiComm.Recv(solidInterfaceBuffRcv_Y, iProc, tag=2)
                    self.mpiComm.Recv(solidInterfaceBuffRcv_Z, iProc, tag=3)
                    self.fillMatrixA(solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, iProc)
        else:
            localSolidInterface_array_X, localSolidInterface_array_Y, localSolidInterface_array_Z = self.SolidSolver.getNodalInitialPositions()
            self.fillMatrixA(localSolidInterface_array_X, localSolidInterface_array_Y, localSolidInterface_array_Z, 0)

        mpiBarrier(self.mpiComm)
        mpiPrint("\nAssembling A...", self.mpiComm)
        start = tm.time()
        self.A.assemble()
        mpiBarrier(self.mpiComm)
        stop = tm.time()
        mpiPrint('Assembly performed in {} s'.format(stop-start), self.mpiComm)
        mpiPrint('Matrix A is built.', self.mpiComm)

        mpiPrint('\nBuilding matrix B & D of size {} X {} & {} X {}...'.format(self.nf, self.ns, self.ns, self.nf), self.mpiComm)
        # Fill the matrix B & D
        if self.mpiComm != None:
            for iProc in solidInterfaceProcessors:
                if self.myid == iProc:
                    for jProc in fluidInterfaceProcessors:
                        self.mpiComm.Send(localSolidInterface_array_X, dest=jProc, tag=1)
                        self.mpiComm.Send(localSolidInterface_array_Y, dest=jProc, tag=2)
                        self.mpiComm.Send(localSolidInterface_array_Z, dest=jProc, tag=3)
                if self.myid in fluidInterfaceProcessors:
                    sizeOfBuff = solidPhysicalInterfaceNodesDistribution[iProc]
                    solidInterfaceBuffRcv_X = np.zeros(sizeOfBuff)
                    solidInterfaceBuffRcv_Y = np.zeros(sizeOfBuff)
                    solidInterfaceBuffRcv_Z = np.zeros(sizeOfBuff)
                    self.mpiComm.Recv(solidInterfaceBuffRcv_X, iProc, tag=1)
                    self.mpiComm.Recv(solidInterfaceBuffRcv_Y, iProc, tag=2)
                    self.mpiComm.Recv(solidInterfaceBuffRcv_Z, iProc, tag=3)
                    self.fillMatrixBD(solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, iProc)
        else:
            self.fillMatrixBD(localSolidInterface_array_X, localSolidInterface_array_Y, localSolidInterface_array_Z, 0)

        mpiBarrier(self.mpiComm)
        mpiPrint("\nAssembling B & D...", self.mpiComm)
        start = tm.time()
        self.B.assemble()
        mpiBarrier(self.mpiComm)
        self.D.assemble()
        mpiBarrier(self.mpiComm)
        stop = tm.time()
        mpiPrint('Assembly performed in {} s'.format(stop-start), self.mpiComm)
        mpiPrint('Matrix B & D are built.', self.mpiComm)

        mpiPrint('\nBuilding matrix C of size {} X {}...'.format(self.nf, self.nf), self.mpiComm)
        # Fill the matrix C
        if self.mpiComm != None:
            for iProc in fluidInterfaceProcessors:
                if self.myid == iProc:
                    localFluidInterface_array_X, localFluidInterface_array_Y, localFluidInterface_array_Z = self.FluidSolver.getNodalInitialPositions()
                    for jProc in fluidInterfaceProcessors:
                        self.mpiComm.Send(localFluidInterface_array_X, dest=jProc, tag=1)
                        self.mpiComm.Send(localFluidInterface_array_Y, dest=jProc, tag=2)
                        self.mpiComm.Send(localFluidInterface_array_Z, dest=jProc, tag=3)
                if self.myid in fluidInterfaceProcessors:
                    sizeOfBuff = fluidPhysicalInterfaceNodesDistribution[iProc]
                    fluidInterfaceBuffRcv_X = np.zeros(sizeOfBuff)
                    fluidInterfaceBuffRcv_Y = np.zeros(sizeOfBuff)
                    fluidInterfaceBuffRcv_Z = np.zeros(sizeOfBuff)
                    self.mpiComm.Recv(fluidInterfaceBuffRcv_X, iProc, tag=1)
                    self.mpiComm.Recv(fluidInterfaceBuffRcv_Y, iProc, tag=2)
                    self.mpiComm.Recv(fluidInterfaceBuffRcv_Z, iProc, tag=3)
                    self.fillMatrixC(fluidInterfaceBuffRcv_X, fluidInterfaceBuffRcv_Y, fluidInterfaceBuffRcv_Z, iProc)
        else:
            localFluidInterface_array_X, localFluidInterface_array_Y, localFluidInterface_array_Z = self.FluidSolver.getNodalInitialPositions()
            self.fillMatrixC(localFluidInterface_array_X, localFluidInterface_array_Y, localFluidInterface_array_Z, 0)

        mpiBarrier(self.mpiComm)
        mpiPrint("\nAssembling C...", self.mpiComm)
        start = tm.time()
        self.C.assemble()
        mpiBarrier(self.mpiComm)
        stop = tm.time()
        mpiPrint('Assembly performed in {} s'.format(stop-start), self.mpiComm)
        mpiPrint('Matrix C is built.', self.mpiComm)

        self.SolverA = LinearSolver(self.A, self.mpiComm)
        self.SolverC = LinearSolver(self.C, self.mpiComm)

    def interpolateFluidToSolid(self, fluidInterfaceData, solidInterfaceData):
        """
        des.
        """

        dim = fluidInterfaceData.getDim()
        gamma_array = FlexInterfaceData(self.nf + self.d, dim, self.mpiComm)

        self.SolverC.solve(fluidInterfaceData, gamma_array)
        self.D.mult(gamma_array, solidInterfaceData)

    def interpolateSolidToFluid(self, solidInterfaceData, fluidInterfaceData):
        """
        Des.
        """

        dim = solidInterfaceData.getDim()
        gamma_array = FlexInterfaceData(self.ns + self.d, dim, self.mpiComm)

        self.SolverA.solve(solidInterfaceData, gamma_array)
        self.B.mult(gamma_array, fluidInterfaceData)

class RBFInterpolator(ConservativeInterpolator):
    """
    Description.
    """

    def __init__(self, Manager, FluidSolver, SolidSolver, RBFradius=0.1, mpiComm = None, chtTransferMethod=None, heatTransferCoeff=1.0):
        """"
        Description.
        """

        ConservativeInterpolator.__init__(self, Manager, FluidSolver, SolidSolver, mpiComm, chtTransferMethod, heatTransferCoeff)

        mpiPrint('\nSetting interpolation with Radial Basis Functions...', mpiComm)

        self.radius = RBFradius

        self.generateInterfaceData()

        self.generateMapping()


    def generateInterfaceData(self):
        """
        Des.
        """

        ConservativeInterpolator.generateInterfaceData(self)

        mpiPrint('Generating interface data for conservative RBF interpolator...', self.mpiComm)

        self.A.createSparseFullAlloc()
        self.A_T.createSparseFullAlloc()
        self.B.createSparseFullAlloc()
        self.B_T.createSparseFullAlloc()

    def fillMatrixA(self, solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, iProc):
        """
        Description.
        """

        localSolidInterface_array_X_init, localSolidInterface_array_Y_init, localSolidInterface_array_Z_init = self.SolidSolver.getNodalInitialPositions()
        start = tm.time()
        ccupydo.CInterpolator.RBF_fillMatrixA(self, localSolidInterface_array_X_init, localSolidInterface_array_Y_init, localSolidInterface_array_Z_init,
                                              solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, self.A, self.A_T, iProc, 1.01*self.radius)
        stop = tm.time()
        print('Built A on rank {} in {} s'.format(self.myid,stop-start))


    def fillMatrixB(self, solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, iProc):
        """
        Description.
        """

        localFluidInterface_array_X_init, localFluidInterface_array_Y_init, localFluidInterface_array_Z_init = self.FluidSolver.getNodalInitialPositions()
        start = tm.time()
        ccupydo.CInterpolator.RBF_fillMatrixB(self, localFluidInterface_array_X_init, localFluidInterface_array_Y_init, localFluidInterface_array_Z_init,
                                              solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, self.B, self.B_T, iProc, 1.01*self.radius)
        stop = tm.time()
        print('Built B on rank {} in {} s'.format(self.myid,stop-start))



class ConsistentRBFInterpolator(ConsistentInterpolator):
    """
    Description.
    """

    def __init__(self, Manager, FluidSolver, SolidSolver, RBFradius = 0.1, mpiComm= None, chtTransferMethod=None, heatTransferCoeff=1.0):
        """
        Des.
        """

        ConsistentInterpolator.__init__(self, Manager, FluidSolver, SolidSolver, mpiComm, chtTransferMethod, heatTransferCoeff)

        mpiPrint('\nSetting interpolation with Radial Basis Functions...', mpiComm)

        self.radius = RBFradius

        self.generateInterfaceData()

        self.generateMapping()

    def generateInterfaceData(self):
        """
        Des.
        """

        ConsistentInterpolator.generateInterfaceData(self)

        mpiPrint('Generating interface data for consistent RBF interpolator...', self.mpiComm)

        self.A.createSparseFullAlloc()
        self.B.createSparseFullAlloc()
        self.C.createSparseFullAlloc()
        self.D.createSparseFullAlloc()


    def fillMatrixA(self, solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, iProc):
        """
        Description.
        """

        localSolidInterface_array_X_init, localSolidInterface_array_Y_init, localSolidInterface_array_Z_init = self.SolidSolver.getNodalInitialPositions()
        start = tm.time()
        ccupydo.CInterpolator.consistent_RBF_fillMatrixA(self, localSolidInterface_array_X_init, localSolidInterface_array_Y_init, localSolidInterface_array_Z_init,
                                              solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, self.A, iProc, 1.01*self.radius)
        stop = tm.time()
        print('Built A on rank {} in {} s'.format(self.myid,stop-start))

    def fillMatrixBD(self, solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, iProc):
        """
        Description.
        """

        localFluidInterface_array_X_init, localFluidInterface_array_Y_init, localFluidInterface_array_Z_init = self.FluidSolver.getNodalInitialPositions()
        start = tm.time()
        ccupydo.CInterpolator.consistent_RBF_fillMatrixBD(self, localFluidInterface_array_X_init, localFluidInterface_array_Y_init, localFluidInterface_array_Z_init,
                                              solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, self.B, self.D, iProc, 1.01*self.radius)
        stop = tm.time()
        print('Built B & D on rank {} in {} s'.format(self.myid,stop-start))

    def fillMatrixC(self, fluidInterfaceBuffRcv_X, fluidInterfaceBuffRcv_Y, fluidInterfaceBuffRcv_Z, iProc):
        """
        Description.
        """

        localFluidInterface_array_X_init, localFluidInterface_array_Y_init, localFluidInterface_array_Z_init = self.FluidSolver.getNodalInitialPositions()
        start = tm.time()
        ccupydo.CInterpolator.consistent_RBF_fillMatrixC(self, localFluidInterface_array_X_init, localFluidInterface_array_Y_init, localFluidInterface_array_Z_init,
                                              fluidInterfaceBuffRcv_X, fluidInterfaceBuffRcv_Y, fluidInterfaceBuffRcv_Z, self.C, iProc, 1.01*self.radius)
        stop = tm.time()
        print('Built C on rank {} in {} s'.format(self.myid,stop-start))

class TPSInterpolator(ConservativeInterpolator):
    """
    Des.
    """

    def __init__(self, Manager, FluidSolver, SolidSolver, mpiComm=None, chtTransferMethod=None, heatTransferCoeff=1.0):
        """
        des.
        """

        ConservativeInterpolator.__init__(self, Manager, FluidSolver, SolidSolver, mpiComm, chtTransferMethod, heatTransferCoeff)

        mpiPrint('\nSetting interpolation with Thin Plate Spline...', self.mpiComm)

        self.generateInterfaceData()

        self.generateMapping()

    def generateInterfaceData(self):
        """
        Des.
        """

        ConservativeInterpolator.generateInterfaceData(self)

        mpiPrint('Generating interface data for TPS interpolator...', self.mpiComm)

        self.A.createDense()
        self.A_T.createDense()
        self.B.createDense()
        self.B_T.createDense()

    def fillMatrixA(self, solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, iProc):
        """
        Description.
        """

        localSolidInterface_array_X_init, localSolidInterface_array_Y_init, localSolidInterface_array_Z_init = self.SolidSolver.getNodalInitialPositions()

        start = tm.time()
        ccupydo.CInterpolator.TPS_fillMatrixA(self, localSolidInterface_array_X_init, localSolidInterface_array_Y_init, localSolidInterface_array_Z_init,
                                              solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, self.A, self.A_T, iProc)
        stop = tm.time()
        print('Built A on rank {} in {} s'.format(self.myid,stop-start))

    def fillMatrixB(self, solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, iProc):
        """
        Description.
        """

        localFluidInterface_array_X_init, localFluidInterface_array_Y_init, localFluidInterface_array_Z_init = self.FluidSolver.getNodalInitialPositions()

        start = tm.time()
        ccupydo.CInterpolator.TPS_fillMatrixB(self, localFluidInterface_array_X_init, localFluidInterface_array_Y_init, localFluidInterface_array_Z_init,
                                              solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, self.B, self.B_T, iProc)
        stop = tm.time()
        print('Built B on rank {} in {} s'.format(self.myid,stop-start))

class ConsistentTPSInterpolator(ConsistentInterpolator):
    """
    Description.
    """

    def __init__(self, Manager, FluidSolver, SolidSolver, mpiComm= None, chtTransferMethod=None, heatTransferCoeff=1.0):
        """
        Des.
        """

        ConsistentInterpolator.__init__(self, Manager, FluidSolver, SolidSolver, mpiComm, chtTransferMethod, heatTransferCoeff)

        mpiPrint('\nSetting consistent interpolation with Thin Plate Spline...', self.mpiComm)

        self.generateInterfaceData()

        self.generateMapping()

    def generateInterfaceData(self):
        """
        Des.
        """

        ConsistentInterpolator.generateInterfaceData(self)

        mpiPrint('Generating interface data for consistent TPS interpolator...', self.mpiComm)

        self.A.createDense()
        self.B.createDense()
        self.C.createDense()
        self.D.createDense()

    def fillMatrixA(self, solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, iProc):
        """
        Des.
        """

        localSolidInterface_array_X_init, localSolidInterface_array_Y_init, localSolidInterface_array_Z_init = self.SolidSolver.getNodalInitialPositions()
        start = tm.time()
        ccupydo.CInterpolator.consistent_TPS_fillMatrixA(self, localSolidInterface_array_X_init, localSolidInterface_array_Y_init, localSolidInterface_array_Z_init,
                                              solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, self.A, iProc)
        stop = tm.time()
        print('Built A on rank {} in {} s'.format(self.myid,stop-start))

    def fillMatrixBD(self, solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, iProc):
        """
        des.
        """

        localFluidInterface_array_X_init, localFluidInterface_array_Y_init, localFluidInterface_array_Z_init = self.FluidSolver.getNodalInitialPositions()
        start = tm.time()
        ccupydo.CInterpolator.consistent_TPS_fillMatrixBD(self, localFluidInterface_array_X_init, localFluidInterface_array_Y_init, localFluidInterface_array_Z_init,
                                              solidInterfaceBuffRcv_X, solidInterfaceBuffRcv_Y, solidInterfaceBuffRcv_Z, self.B, self.D, iProc)
        stop = tm.time()
        print('Built B & D on rank {} in {} s'.format(self.myid,stop-start))

    def fillMatrixC(self, fluidInterfaceBuffRcv_X, fluidInterfaceBuffRcv_Y, fluidInterfaceBuffRcv_Z, iProc):
        """
        Des.
        """

        localFluidInterface_array_X_init, localFluidInterface_array_Y_init, localFluidInterface_array_Z_init = self.FluidSolver.getNodalInitialPositions()
        start = tm.time()
        ccupydo.CInterpolator.consistent_TPS_fillMatrixC(self, localFluidInterface_array_X_init, localFluidInterface_array_Y_init, localFluidInterface_array_Z_init,
                                              fluidInterfaceBuffRcv_X, fluidInterfaceBuffRcv_Y, fluidInterfaceBuffRcv_Z, self.C, iProc)
        stop = tm.time()
        print('Built C on rank {} in {} s'.format(self.myid,stop-start))

# ----------------------------------------------------------------------
#    Algorithm class
# ----------------------------------------------------------------------

class Algorithm:
    """
    Des.
    """

    def __init__(self, Manager, FluidSolver, SolidSolver, InterfaceInterpolator, Criterion, nbFSIIterMax, deltaT, totTime, timeIterTreshold=-1, mpiComm=None):
        """
        Des.
        """

        mpiPrint('\n***************************** Initializing FSI algorithm *****************************', mpiComm)

        self.mpiComm = mpiComm
        self.manager = Manager
        self.FluidSolver = FluidSolver
        self.SolidSolver = SolidSolver
        self.interfaceInterpolator = InterfaceInterpolator
        self.criterion = Criterion

        self.globalTimer = Timer()
        self.communicationTimer = Timer()
        self.meshDefTimer = Timer()
        self.fluidSolverTimer = Timer()
        self.solidSolverTimer = Timer()
        self.solidRemeshingTimer = Timer()
        self.fluidRemeshingTimer = Timer()

        self.nbFSIIterMax = nbFSIIterMax
        self.deltaT = deltaT
        self.totTime = totTime
        self.timeIterTreshold = timeIterTreshold
        self.writeInFSIloop = False
        
        self.time = 0.0
        self.timeIter = 0
        
        self.FSIIter = 0
        self.errValue = 0.0
        self.FSIConv = False
        self.totNbOfFSIIt = 0

        if self.mpiComm != None:
            self.myid = self.mpiComm.Get_rank()
            self.mpiSize = self.mpiComm.Get_size()
        else:
            self.myid = 0
            self.mpiSize = 1

        self.alpha_0 = 1.0
        self.alpha_1 = 0.5

        ns = self.interfaceInterpolator.getNs()
        d = self.interfaceInterpolator.getd()

        self.solidInterfaceVelocity = FlexInterfaceData(ns+d, 3, self.mpiComm)
        self.solidInterfaceVelocitynM1 = FlexInterfaceData(ns+d, 3, self.mpiComm)

        self.solidInterfaceResidual = FlexInterfaceData(ns+d, 3, self.mpiComm)

        self.solidHeatFluxResidual = None
        self.solidTemperatureResidual = None
        if self.manager.withCht:
            self.solidHeatFluxResidual = FlexInterfaceData(ns+d, 3, self.mpiComm)
            self.solidTemperatureResidual = FlexInterfaceData(ns+d, 1, self.mpiComm)

    def setFSIInitialConditions(self):
        """
        Des.
        """

        if self.manager.computationType == 'unsteady':
            if self.myid in self.manager.getSolidSolverProcessors():
                self.SolidSolver.setInitialDisplacements()
            self.interfaceInterpolator.getDisplacementFromSolidSolver()
            self.interfaceInterpolator.interpolateSolidDisplacementOnFluidMesh()
            self.interfaceInterpolator.setDisplacementToFluidSolver(self.time)
            self.FluidSolver.setInitialMeshDeformation()
            if self.manager.withCht:
                if self.interfaceInterpolator.chtTransferMethod == 'hFFB' or self.interfaceInterpolator.chtTransferMethod == 'TFFB':
                    #self.interfaceInterpolator.getHeatFluxFromSolidSolver()
                    #self.interfaceInterpolator.interpolateSolidHeatFluxOnFluidMesh()
                    #self.interfaceInterpolator.setHeatFluxToFluidSolver(self.time)
                    self.FluidSolver.setInitialInterfaceHeatFlux()
                elif self.interfaceInterpolator.chtTransferMethod == 'hFTB' or self.interfaceInterpolator.chtTransferMethod == 'FFTB':
                    self.FluidSolver.setInitialInterfaceTemperature()
        else:
            self.interfaceInterpolator.getDisplacementFromSolidSolver()
            if self.manager.withCht:
                if self.interfaceInterpolator.chtTransferMethod == 'hFFB' or self.interfaceInterpolator.chtTransferMethod == 'TFFB':
                    #self.interfaceInterpolator.getHeatFluxFromSolidSolver()
                    #self.interfaceInterpolator.interpolateSolidHeatFluxOnFluidMesh()
                    #self.interfaceInterpolator.setHeatFluxToFluidSolver(self.time)
                    self.FluidSolver.setInitialInterfaceHeatFlux()
                elif self.interfaceInterpolator.chtTransferMethod == 'hFTB' or self.interfaceInterpolator.chtTransferMethod == 'FFTB':
                    self.FluidSolver.setInitialInterfaceTemperature()

    def computeSolidInterfaceResidual(self):
        """
        Des.
        """

        ns = self.interfaceInterpolator.getNs()
        d = self.interfaceInterpolator.getd()

        # --- Get the predicted (computed) solid interface displacement from the solid solver --- #
        predictedDisplacement = FlexInterfaceData(ns+d, 3, self.mpiComm)

        if self.myid in self.manager.getSolidInterfaceProcessors():
            localSolidInterfaceDisp_X, localSolidInterfaceDisp_Y, localSolidInterfaceDisp_Z = self.SolidSolver.getNodalDisplacements()
            for iVertex in range(self.manager.getNumberOfLocalSolidInterfaceNodes()):
                iGlobalVertex = self.manager.getGlobalIndex('solid', self.myid, iVertex)
                predictedDisplacement[iGlobalVertex] = [localSolidInterfaceDisp_X[iVertex], localSolidInterfaceDisp_Y[iVertex], localSolidInterfaceDisp_Z[iVertex]]

        predictedDisplacement.assemble()

        # --- Calculate the residual (vector and norm) --- #
        mpiPrint("\nCompute FSI residual based on solid interface displacement.", self.mpiComm)
        #self.solidInterfaceResidual = predictedDisplacement - self.interfaceInterpolator.solidInterfaceDisplacement
        self.solidInterfaceResidual.set(predictedDisplacement - self.interfaceInterpolator.solidInterfaceDisplacement)

        return self.solidInterfaceResidual

    def computeSolidInterfaceResidual_CHT(self):
        """
        Des.
        """

        ns = self.interfaceInterpolator.getNs()
        d = self.interfaceInterpolator.getd()

        predictedHF = FlexInterfaceData(ns+d, 3, self.mpiComm)
        predictedTemp = FlexInterfaceData(ns+d, 1, self.mpiComm)

        if self.myid in self.manager.getSolidInterfaceProcessors():
            localSolidInterfaceHeatFlux_X, localSolidInterfaceHeatFlux_Y, localSolidInterfaceHeatFlux_Z = self.SolidSolver.getNodalHeatFluxes()
            localSolidInterfaceTemperature = self.SolidSolver.getNodalTemperatures()
            for iVertex in range(self.manager.getNumberOfLocalSolidInterfaceNodes()):
                iGlobalVertex = self.manager.getGlobalIndex('solid', self.myid, iVertex)
                predictedHF[iGlobalVertex] = [localSolidInterfaceHeatFlux_X[iVertex], localSolidInterfaceHeatFlux_Y[iVertex], localSolidInterfaceHeatFlux_Z[iVertex]]
                predictedTemp[iGlobalVertex] = [localSolidInterfaceTemperature[iVertex]]

        predictedHF.assemble()
        predictedTemp.assemble()

        if self.interfaceInterpolator.chtTransferMethod == 'hFFB' or self.interfaceInterpolator.chtTransferMethod == 'TFFB':
            mpiPrint("\nCompute CHT residual based on solid interface heat flux.", self.mpiComm)
            #self.solidHeatFluxResidual = predictedHF - self.interfaceInterpolator.solidInterfaceHeatFlux
            self.solidHeatFluxResidual.set(predictedHF - self.interfaceInterpolator.solidInterfaceHeatFlux)
            return self.solidHeatFluxResidual
        elif self.interfaceInterpolator.chtTransferMethod == 'hFTB' or self.interfaceInterpolator.chtTransferMethod == 'FFTB':
            mpiPrint("\nCompute CHT residual based on solid interface temperature.", self.mpiComm)
            #self.solidTemperatureResidual = predictedTemp - self.interfaceInterpolator.solidInterfaceTemperature
            self.solidTemperatureResidual.set(predictedTemp - self.interfaceInterpolator.solidInterfaceTemperature)
            return self.solidTemperatureResidual
        else:
            return None

    def solidDisplacementPredictor(self):
        """
        Des
        """

        # --- Get the velocity (current and previous time step) of the solid interface from the solid solver --- #
        if self.myid in self.manager.getSolidInterfaceProcessors():
            localSolidInterfaceVel_X, localSolidInterfaceVel_Y, localSolidInterfaceVel_Z = self.SolidSolver.getNodalVelocity()
            localSolidInterfaceVelNm1_X, localSolidInterfaceVelNm1_Y, localSolidInterfaceVelNm1_Z = self.SolidSolver.getNodalVelocityNm1()
            for iVertex in range(self.manager.getNumberOfLocalSolidInterfaceNodes()):
                iGlobalVertex = self.manager.getGlobalIndex('solid', self.myid, iVertex)
                self.solidInterfaceVelocity[iGlobalVertex] = [localSolidInterfaceVel_X[iVertex], localSolidInterfaceVel_Y[iVertex], localSolidInterfaceVel_Z[iVertex]]
                self.solidInterfaceVelocitynM1[iGlobalVertex] = [localSolidInterfaceVelNm1_X[iVertex], localSolidInterfaceVelNm1_Y[iVertex], localSolidInterfaceVelNm1_Z[iVertex]]

        self.solidInterfaceVelocity.assemble()
        self.solidInterfaceVelocitynM1.assemble()

        # --- Predict the solid position for the next time step --- #
        self.interfaceInterpolator.solidInterfaceDisplacement += (self.alpha_0*self.deltaT*self.solidInterfaceVelocity + self.alpha_1*self.deltaT*(self.solidInterfaceVelocity-self.solidInterfaceVelocitynM1))

    def fluidToSolidMechaTransfer(self):
        """
        Des.
        """

        self.communicationTimer.start()
        self.interfaceInterpolator.getLoadsFromFluidSolver()
        self.interfaceInterpolator.interpolateFluidLoadsOnSolidMesh()
        self.interfaceInterpolator.setLoadsToSolidSolver(self.time)
        self.communicationTimer.stop()
        self.communicationTimer.cumul()

    def solidToFluidMechaTransfer(self):
        """
        Des.
        """

        self.communicationTimer.start()
        self.interfaceInterpolator.interpolateSolidDisplacementOnFluidMesh()
        self.interfaceInterpolator.setDisplacementToFluidSolver(self.time)
        self.communicationTimer.stop()
        self.communicationTimer.cumul()

    def solidToFluidThermalTransfer(self):
        """
        Des.
        """

        self.communicationTimer.start()
        if self.interfaceInterpolator.chtTransferMethod == 'TFFB' or self.interfaceInterpolator.chtTransferMethod == 'hFFB':
            self.interfaceInterpolator.interpolateSolidHeatFluxOnFluidMesh()
            self.interfaceInterpolator.setHeatFluxToFluidSolver(self.time)
        elif self.interfaceInterpolator.chtTransferMethod == 'FFTB' or self.interfaceInterpolator.chtTransferMethod == 'hFTB':
            self.interfaceInterpolator.interpolateSolidTemperatureOnFluidMesh()
            self.interfaceInterpolator.setTemperatureToFluidSolver(self.time)
        self.communicationTimer.stop()
        self.communicationTimer.cumul()

    def fluidToSolidThermalTransfer(self):
        """
        Des.
        """

        self.communicationTimer.start()
        if self.interfaceInterpolator.chtTransferMethod == 'TFFB':
            self.interfaceInterpolator.getTemperatureFromFluidSolver()
            self.interfaceInterpolator.interpolateFluidTemperatureOnSolidMesh()
            self.interfaceInterpolator.setTemperatureToSolidSolver(self.time)
        elif self.interfaceInterpolator.chtTransferMethod == 'FFTB':
            self.interfaceInterpolator.getHeatFluxFromFluidSolver()
            self.interfaceInterpolator.interpolateFluidHeatFluxOnSolidMesh()
            self.interfaceInterpolator.setHeatFluxToSolidSolver(self.time)
        elif self.interfaceInterpolator.chtTransferMethod == 'hFFB' or self.interfaceInterpolator.chtTransferMethod == 'hFTB':
            self.interfaceInterpolator.getRobinTemperatureFromFluidSolver()
            self.interfaceInterpolator.interpolateFluidRobinTemperatureOnSolidMesh()
            self.interfaceInterpolator.setRobinHeatFluxToSolidSolver(self.time)
        self.communicationTimer.stop()
        self.communicationTimer.cumul()

    def iniRealTimeData(self):
        """
        Des
        """

        if self.myid in self.manager.getSolidSolverProcessors():
            self.SolidSolver.initRealTimeData()
        histFile = open('FSIhistory.ascii', "w")
        histFile.write("TimeIter\tTime\tFSIError\tCHTError\tFSINbIter\n")
        histFile.close()

    def writeRealTimeData(self):
        """
        Des
        """

        if self.myid == 0:
            self.FluidSolver.saveRealTimeData(self.time, self.FSIIter)
            if self.timeIter >= self.timeIterTreshold:
                self.SolidSolver.saveRealTimeData(self.time, self.FSIIter)
            histFile = open('FSIhistory.ascii', "a")
            histFile.write(str(self.timeIter) + '\t' + str(self.time) + '\t' + str(self.errValue) + '\t' + str(self.errValue_CHT) + '\t' + str(self.FSIIter) + '\n')
            histFile.close()

    def getMeanNbOfFSIIt(self):
        """
        Des
        """
        if self.manager.computationType == 'unsteady':
            if self.timeIter > 1:
                return float(self.totNbOfFSIIt)/(self.timeIter-1)
            else:
                return 0.0
        else:
            return self.FSIIter

    def printExitInfo(self):
        """
        Des
        """

        mpiPrint('[cpu FSI total]: ' + str(self.globalTimer.cumulTime) + ' s', self.mpiComm)
        mpiPrint('[cpu FSI fluid mesh mapping]: ' + str(self.interfaceInterpolator.mappingTimer.cumulTime) + ' s', self.mpiComm)
        mpiPrint('[cpu FSI fluid mesh deformation]: ' + str(self.meshDefTimer.cumulTime) + ' s', self.mpiComm)
        mpiPrint('[cpu FSI communications]: ' + str(self.communicationTimer.cumulTime) + ' s', self.mpiComm)
        mpiPrint('[cpu FSI fluid solver]: ' + str(self.fluidSolverTimer.cumulTime) + ' s', self.mpiComm)
        mpiPrint('[cpu FSI solid solver]: ' + str(self.solidSolverTimer.cumulTime) + ' s', self.mpiComm)
        mpiPrint('[cpu FSI fluid remeshing]: ' + str(self.fluidRemeshingTimer.cumulTime) + ' s', self.mpiComm)
        mpiPrint('[cpu FSI solid remeshing]: ' + str(self.solidRemeshingTimer.cumulTime) + ' s', self.mpiComm)
        mpiPrint('[Time steps FSI]: ' + str(self.timeIter), self.mpiComm)
        mpiPrint('[Successful Run FSI]: ' + str(self.time >= (self.totTime - 2*self.deltaT)), self.mpiComm) # NB: self.totTime - 2*self.deltaT is the extreme case that can be encountered due to rounding effects!
        mpiPrint('[Mean n. of FSI Iterations]: ' + str(self.getMeanNbOfFSIIt()), self.mpiComm)

        if self.myid == 0 :
            self.FluidSolver.printRealTimeData(self.time, self.FSIIter)
            self.SolidSolver.printRealTimeData(self.time, self.FSIIter)

        mpiPrint('RES-FSI-FSIhistory: ' + str(self.timeIter) + '\t' + str(self.time) + '\t' + str(self.errValue) + '\t' + str(self.FSIIter) + '\n', self.mpiComm)

    def run(self):
        """
        Des.
        """
        
        # --- Initialize output manager --- #
        self.iniRealTimeData()

        mpiPrint('\n**********************************', self.mpiComm)
        mpiPrint('*         Begin FSI computation            *', self.mpiComm)
        mpiPrint('**********************************\n', self.mpiComm)

        self.globalTimer.start()

        #If no restart
        mpiPrint('Setting FSI initial conditions...', self.mpiComm)
        self.setFSIInitialConditions()
        mpiPrint('\nFSI initial conditions are set', self.mpiComm)
        
        try:
            if self.manager.computationType == 'unsteady':
                self.__unsteadyRun()
            else:
                self.time = self.totTime
                self.timeIter = 1
                self.deltaT = self.totTime
                self.writeInFSIloop = True
                self.fsiCoupling()
                self.totNbOfFSIIt = self.FSIIter
        except:
            mpiPrint('\nA DIVINE ERROR OCCURED...EXITING COMPUTATION\n', self.mpiComm)
            traceback.print_exc()
        finally:
            self.globalTimer.stop()
            self.globalTimer.cumul()
            
            mpiBarrier(self.mpiComm)
            
            mpiPrint('\n*************************', self.mpiComm)
            mpiPrint('*    End FSI computation    *', self.mpiComm)
            mpiPrint('*************************\n', self.mpiComm)
            
            self.printExitInfo()
            
            # --- Exit the solid solver --- #
            if self.myid in self.manager.getSolidSolverProcessors():
                self.SolidSolver.exit()

            # --- Exit the fluid solver --- #
            self.FluidSolver.exit()
    
            # --- Exit computation --- #
            mpiBarrier(self.mpiComm)

    def __unsteadyRun(self):
        """
        Des.
        """

        #If no restart
        nbTimeIter = int((self.totTime/self.deltaT)-1)

        mpiPrint('Begin time integration\n', self.mpiComm)

        # --- External temporal loop --- #
        while self.timeIter <= nbTimeIter:
            
            mpiPrint("\n>>>> Time iteration {} <<<<".format(self.timeIter), self.mpiComm)

            # --- Preprocess the temporal iteration --- #
            self.FluidSolver.preprocessTimeIter(self.timeIter)
            if self.myid in self.manager.getSolidSolverProcessors():
                self.SolidSolver.preprocessTimeIter(self.timeIter)

            # --- Internal FSI loop --- #
            self.fsiCoupling()
            # --- End of FSI loop --- #

            mpiBarrier(self.mpiComm)
            
            if self.timeIter > 0:
                self.totNbOfFSIIt += self.FSIIter

            # --- Update the fluid and solid solver for the next time step --- #
            if self.myid in self.manager.getSolidSolverProcessors():
                self.SolidSolver.update()
            self.FluidSolver.update(self.deltaT)

            # --- Write fluid and solid solution, update FSI history  ---#
            self.FluidSolver.save(self.timeIter)

            if self.myid in self.manager.getSolidSolverProcessors():
                self.SolidSolver.save()

            self.writeRealTimeData()

            # --- Perform some remeshing if necessary
            if self.myid in self.manager.getSolidSolverProcessors():
                self.solidRemeshingTimer.start()
                self.SolidSolver.remeshing()
                self.solidRemeshingTimer.stop()
                self.solidRemeshingTimer.cumul()
            
            self.fluidRemeshingTimer.start()
            self.FluidSolver.remeshing()
            self.fluidRemeshingTimer.stop()
            self.fluidRemeshingTimer.cumul()
            # ---

            if self.timeIter >= self.timeIterTreshold:
                # --- Displacement predictor for the next time step and update of the solid solution --- #
                mpiPrint('\nSolid displacement prediction for next time step', self.mpiComm)
                self.solidDisplacementPredictor()

            self.timeIter += 1
            self.time += self.deltaT
        # --- End of the temporal loop --- #

class AlgorithmBGSStaticRelax(Algorithm):
    """
    Des.
    """

    def __init__(self, Manager, FluidSolver, SolidSolver, InterfaceInterpolator, Criterion, nbFSIIterMax, deltaT, totTime, timeIterTreshold=-1, omegaMax=1.0, mpiComm=None):
        """
        Des.
        """

        Algorithm.__init__(self, Manager, FluidSolver, SolidSolver, InterfaceInterpolator, Criterion, nbFSIIterMax, deltaT, totTime, timeIterTreshold, mpiComm)

        self.omegaMax = omegaMax
        self.omegaMin = 1e-12
        self.omega = omegaMax

    def fsiCoupling(self):
        """
        Block Gauss Seidel (BGS) method for strong coupling FSI
        """

        if self.timeIter > self.timeIterTreshold:
            nbFSIIter = self.nbFSIIterMax
            mpiPrint('\n*************** Enter Block Gauss Seidel (BGS) method for strong coupling FSI ***************', self.mpiComm)
        else:
             nbFSIIter = 1

        self.FSIIter = 0
        self.FSIConv = False
        self.errValue = 1e12
        self.errValue_CHT = 1e6

        solidHasRun = False

        while ((self.FSIIter < nbFSIIter) and (not self.criterion.isVerified(self.errValue, self.errValue_CHT))):
            mpiPrint("\n>>>> FSI iteration {} <<<<\n".format(self.FSIIter), self.mpiComm)

            # --- Solid to fluid mechanical transfer --- #
            self.solidToFluidMechaTransfer()
            # --- Fluid mesh morphing --- #
            mpiPrint('\nPerforming mesh deformation...\n', self.mpiComm)
            self.meshDefTimer.start()
            self.FluidSolver.meshUpdate(self.timeIter)
            self.meshDefTimer.stop()
            self.meshDefTimer.cumul()
            # --- Solid to fluid thermal transfer --- #
            if self.manager.withCht and solidHasRun:
                self.solidToFluidThermalTransfer()

            # --- Fluid solver call for FSI subiteration --- #
            mpiPrint('\nLaunching fluid solver...', self.mpiComm)
            self.fluidSolverTimer.start()
            self.FluidSolver.run(self.time-self.deltaT, self.time)
            self.fluidSolverTimer.stop()
            self.fluidSolverTimer.cumul()
            mpiBarrier(self.mpiComm)

            if self.timeIter > self.timeIterTreshold:
                # --- Fluid to solid mechanical transfer --- #
                mpiPrint('\nProcessing interface fluid loads...\n', self.mpiComm)
                self.fluidToSolidMechaTransfer()
                if self.manager.withCht:
                    # --- Fluid to solid thermal transfer --- #
                    self.fluidToSolidThermalTransfer()
                mpiBarrier(self.mpiComm)

                # --- Solid solver call for FSI subiteration --- #
                mpiPrint('\nLaunching solid solver...\n', self.mpiComm)
                if self.myid in self.manager.getSolidSolverProcessors():
                    self.solidSolverTimer.start()
                    self.SolidSolver.run(self.time-self.deltaT, self.time)
                    self.solidSolverTimer.stop()
                    self.solidSolverTimer.cumul()
                solidHasRun = True

                # --- Compute the mechanical residual --- #
                res = self.computeSolidInterfaceResidual()
                self.errValue = self.criterion.update(res)
                mpiPrint('\nFSI error value : {}\n'.format(self.errValue), self.mpiComm)
                if self.manager.withCht:
                    # --- Compute the thermal residual --- #
                    res_CHT = self.computeSolidInterfaceResidual_CHT()
                    self.errValue_CHT = self.criterion.updateHeatFlux(res_CHT)
                    mpiPrint('\nCHT error value : {}\n'.format(self.errValue_CHT), self.mpiComm)
                # --- Monitor the coupling convergence --- #
                self.FSIConv = self.criterion.isVerified(self.errValue, self.errValue_CHT)

                # --- Relaxe the solid position --- #
                mpiPrint('\nProcessing interface displacements...\n', self.mpiComm)
                self.relaxSolidPosition()
                # --- Relaxe thermal data --- #
                if self.manager.withCht:
                    self.relaxCHT()

            if self.writeInFSIloop == True:
                self.writeRealTimeData()

            self.FSIIter += 1
            if self.manager.computationType != 'unsteady':
                self.time += self.deltaT

            # --- Update the solvers for the next BGS iteration --- #
            if self.myid in self.manager.getSolidSolverProcessors():
                self.SolidSolver.bgsUpdate()
            self.FluidSolver.bgsUpdate()

        if self.timeIter > self.timeIterTreshold:
            mpiPrint('\n*************** BGS is converged ***************', self.mpiComm)

    def setOmega(self):
        """
        Des.
        """

        self.omega = self.omegaMax

        mpiPrint('Static under-relaxation step with parameter {}'.format(self.omega), self.mpiComm)

    def relaxSolidPosition(self):
        """
        Des.
        """

        # --- Set the relaxation parameter --- #
        self.setOmega()

        # --- Relax the solid interface position --- #
        self.interfaceInterpolator.solidInterfaceDisplacement += (self.omega*self.solidInterfaceResidual)

    def relaxCHT(self):
        """
        Des.
        """

        if self.interfaceInterpolator.chtTransferMethod == 'hFFB' or self.interfaceInterpolator.chtTransferMethod == 'TFFB':
            self.interfaceInterpolator.solidInterfaceHeatFlux += self.solidHeatFluxResidual
        elif self.interfaceInterpolator.chtTransferMethod == 'hFTB' or self.interfaceInterpolator.chtTransferMethod == 'FFTB':
            self.interfaceInterpolator.solidInterfaceTemperature += self.solidTemperatureResidual

class AlgorithmBGSAitkenRelax(AlgorithmBGSStaticRelax):

    def __init__(self, Manager, FluidSolver, SolidSolver, InterfaceInterpolator, Criterion, nbFSIIterMax, deltaT, totTime, timeIterTreshold=-1, omegaMax=1.0, mpiComm=None):
        """
        Des.
        """

        AlgorithmBGSStaticRelax.__init__(self, Manager, FluidSolver, SolidSolver, InterfaceInterpolator, Criterion, nbFSIIterMax, deltaT, totTime, timeIterTreshold, omegaMax, mpiComm)

        ns = self.interfaceInterpolator.getNs()
        self.solidInterfaceResidualkM1 = FlexInterfaceData(ns, 3, self.mpiComm)
        self.aitkenCrit = 'max'

    def setOmega(self):
        """
        Des.
        """

        if self.FSIIter != 0:
            # --- Compute the dynamic Aitken coefficient --- #
            deltaInterfaceResidual = self.solidInterfaceResidual - self.solidInterfaceResidualkM1

            prodScalRes_X, prodScalRes_Y, prodScalRes_Z = deltaInterfaceResidual.dot(self.solidInterfaceResidualkM1)
            prodScalRes = prodScalRes_X + prodScalRes_Y + prodScalRes_Z

            deltaInterfaceResidual_NormX, deltaInterfaceResidual_NormY, deltaInterfaceResidual_NormZ = deltaInterfaceResidual.norm()
            deltaResNormSquare = deltaInterfaceResidual_NormX**2 + deltaInterfaceResidual_NormY**2 + deltaInterfaceResidual_NormZ**2

            if deltaResNormSquare != 0.:
                self.omega *= -prodScalRes/deltaResNormSquare
            else:
                self.omega = self.omegaMin

        else:
            if self.aitkenCrit == 'max':
                self.omega = max(self.omegaMax, self.omega)
            else:
                self.omega = min(self.omegaMax, self.omega)

        self.omega = min(self.omega, 1.0)
        self.omega = max(self.omega, self.omegaMin)

        mpiPrint('Aitken under-relaxation step with parameter {}'.format(self.omega), self.mpiComm)

        # --- Update the value of the residual for the next FSI iteration --- #
        #self.solidInterfaceResidualkM1 = self.solidInterfaceResidual.copy()
        self.solidInterfaceResidual.copy(self.solidInterfaceResidualkM1)

class AlgorithmIQN_ILS(AlgorithmBGSAitkenRelax):
    """
    Des.
    """

    def __init__(self, Manager, FluidSolver, SolidSolver, InterfaceInterpolator, Criterion, nbFSIIterMax, deltaT, totTime, timeIterTreshold=-1, omegaMax=1.0, nbTimeToKeep=0, computeTangentMatrixBasedOnFirstIt = False, mpiComm=None):
        """
        Des.
        """

        AlgorithmBGSAitkenRelax.__init__(self, Manager, FluidSolver, SolidSolver, InterfaceInterpolator, Criterion, nbFSIIterMax, deltaT, totTime, timeIterTreshold, omegaMax, mpiComm)

        # --- Number of previous time steps used in the approximation of the tangent matrix --- #
        self.nbTimeToKeep = nbTimeToKeep

        # --- Option which allows to build the tangent matrix of a given time step using differences with respect to the first FSI iteration (delta_r_k = r_k+1 - r_0) instead of the previous iteration (delta_r_k = r_k+1 - r_k) --- #
        self.computeTangentMatrixBasedOnFirstIt = computeTangentMatrixBasedOnFirstIt

        # --- Option which determines the way the c coefficients are computed either using Degroote's QR decompoistion or simply using np.linalg.lstsq
        self.useQR = True
        self.tollQR = 1.0e-1 # Tolerance employed for the QR decomposition
        self.qrFilter = 'Haelterman' # Type of QR filtering employed. Possible choices are 'Degroote1', 'Degroote2', and 'Haelterman' (see 'qrSolve()' function below)
        
        self.maxNbOfItReached = False
        self.convergenceReachedInOneIt = False
        
        # --- Global V and W matrices for IQN-ILS algorithm, including information from previous time steps --- #
        self.V = []
        self.W = []
    
    def qrSolve(self, V, W, res):
        
        if self.qrFilter == 'Degroote1': # QR filtering as described by J. Degroote et al. Computers and Structures, 87, 793-801 (2009).
            Q, R = sp.linalg.qr(V, mode='economic')
            s = np.dot(np.transpose(Q), -res)
            toll = self.tollQR*sp.linalg.norm(R, 2)
            c = solve_upper_triangular_mod(R, s, toll)
        
        elif self.qrFilter == 'Degroote2': # QR filtering as described by J. Degroote et al. CMAME, 199, 2085-2098 (2010).
            Q, R, V, W = QRfiltering(V, W, self.tollQR)
            s = np.dot(np.transpose(Q), -res)
            c = np.linalg.solve(R, s)
        
        elif self.qrFilter == 'Haelterman': # 'Modified' QR filtering as described by R. Haelterman et al. Computers and Structures, 171, 9-17 (2016).
            Q, R, V, W = QRfiltering_mod(V, W, self.tollQR)
            s = np.dot(np.transpose(Q), -res)
            c = np.linalg.solve(R, s)
        
        else:
            raise NameError('IQN-ILS Algorithm: the QR filtering technique is unknown!')
        
        return c, W
    
    def fsiCoupling(self):
        """
        Interface Quasi Newton - Inverse Least Square (IQN-ILS) method for strong coupling FSI
        """

        if self.timeIter > self.timeIterTreshold:
            nbFSIIter = self.nbFSIIterMax
            mpiPrint('\n*************** Enter Interface Quasi Newton - Inverse Least Square (IQN-ILS) method for strong coupling FSI ***************', self.mpiComm)
        else:
             nbFSIIter = 1

        self.FSIIter = 0
        self.FSIConv = False
        self.errValue = 1.0
        self.errValue_CHT = 1e6 # Just for compatibility. CHT not yet implemented for the IQN-ILS algorithm.
        
        ns = self.interfaceInterpolator.getNs()

        # --- Initialize all the quantities used in the IQN-ILS method --- #
        res = FlexInterfaceData(ns, 3, self.mpiComm)
        solidInterfaceResidual0 = FlexInterfaceData(ns, 3, self.mpiComm)

        solidInterfaceDisplacement_tilde = FlexInterfaceData(ns, 3, self.mpiComm)
        solidInterfaceDisplacement_tilde1 = FlexInterfaceData(ns, 3, self.mpiComm)

        delta_ds = FlexInterfaceData(ns, 3, self.mpiComm)

        Vk_mat = np.zeros((self.manager.nDim*ns,1))
        Wk_mat = np.zeros((self.manager.nDim*ns,1))

        delta_ds_loc_X = np.zeros(0)
        delta_ds_loc_Y = np.zeros(0)
        delta_ds_loc_Z = np.zeros(0)

        if (self.nbTimeToKeep!=0 and self.timeIter > 1): # If information from previous time steps is re-used then Vk = V, Wk = W
            Vk = copy.deepcopy(self.V)
            Wk = copy.deepcopy(self.W)
        else: # If information from previous time steps is not re-used then Vk and Wk are empty lists of np.array()
            Vk = []
            Wk = []
        
        nIt = 0

        while ((self.FSIIter < nbFSIIter) and (not self.criterion.isVerified(self.errValue,self.errValue_CHT))):
            mpiPrint("\n>>>> FSI iteration {} <<<<\n".format(self.FSIIter), self.mpiComm)

            # --- Solid to fluid mechanical transfer --- #
            self.solidToFluidMechaTransfer()
            # --- Fluid mesh morphing --- #
            mpiPrint('\nPerforming mesh deformation...\n', self.mpiComm)
            self.meshDefTimer.start()
            self.FluidSolver.meshUpdate(self.timeIter)
            self.meshDefTimer.stop()
            self.meshDefTimer.cumul()

            # --- Fluid solver call for FSI subiteration --- #
            mpiPrint('\nLaunching fluid solver...', self.mpiComm)
            self.fluidSolverTimer.start()
            self.FluidSolver.run(self.time-self.deltaT, self.time)
            self.fluidSolverTimer.stop()
            self.fluidSolverTimer.cumul()
            mpiBarrier(self.mpiComm)

            if self.timeIter > self.timeIterTreshold:
                # --- Fluid to solid mechanical transfer --- #
                mpiPrint('\nProcessing interface fluid loads...\n', self.mpiComm)
                self.fluidToSolidMechaTransfer()
                mpiBarrier(self.mpiComm)

                # --- Solid solver call for FSI subiteration --- #
                mpiPrint('\nLaunching solid solver...\n', self.mpiComm)
                if self.myid in self.manager.getSolidSolverProcessors():
                    self.solidSolverTimer.start()
                    self.SolidSolver.run(self.time-self.deltaT, self.time)
                    self.solidSolverTimer.stop()
                    self.solidSolverTimer.cumul()

                # --- Compute and monitor the FSI residual --- #
                res = self.computeSolidInterfaceResidual()
                self.errValue = self.criterion.update(res)
                mpiPrint('\nFSI error value : {}\n'.format(self.errValue), self.mpiComm)
                self.FSIConv = self.criterion.isVerified(self.errValue)

                # --- Initialize d_tilde for the construction of the Wk matrix -- #
                if self.myid in self.manager.getSolidInterfaceProcessors():
                    localSolidInterfaceDisp_X, localSolidInterfaceDisp_Y, localSolidInterfaceDisp_Z = self.SolidSolver.getNodalDisplacements()
                    for iVertex in range(self.manager.getNumberOfLocalSolidInterfaceNodes()):
                        iGlobalVertex = self.manager.getGlobalIndex('solid', self.myid, iVertex)
                        solidInterfaceDisplacement_tilde[iGlobalVertex] = [localSolidInterfaceDisp_X[iVertex], localSolidInterfaceDisp_Y[iVertex], localSolidInterfaceDisp_Z[iVertex]]

                solidInterfaceDisplacement_tilde.assemble()
                
                if ((self.FSIIter == 0 and (self.nbTimeToKeep == 0 or (self.nbTimeToKeep != 0 and (self.maxNbOfItReached or self.convergenceReachedInOneIt or self.timeIter == 1)))) or self.timeIter < 1): # If information from previous time steps is re-used then this step is only performed at the first iteration of the first time step, otherwise it is performed at the first iteration of every time step
                    # --- Relax the solid position --- #
                    mpiPrint('\nProcessing interface displacements...\n', self.mpiComm)
                    self.relaxSolidPosition()
                else:
                    # --- Construct Vk and Wk matrices for the computation of the approximated tangent matrix --- #
                    mpiPrint('\nCorrect solid interface displacements using IQN-ILS method...\n', self.mpiComm)
                    
                    # --- Start gathering on root process --- #
                    res_X_Gat, res_Y_Gat, res_Z_Gat = mpiGatherInterfaceData(res, ns, self.mpiComm, 0)
                    solidInterfaceResidual0_X_Gat, solidInterfaceResidual0_Y_Gat, solidInterfaceResidual0_Z_Gat = mpiGatherInterfaceData(solidInterfaceResidual0, ns, self.mpiComm, 0)
                    solidInterfaceDisplacement_tilde_X_Gat, solidInterfaceDisplacement_tilde_Y_Gat, solidInterfaceDisplacement_tilde_Z_Gat = mpiGatherInterfaceData(solidInterfaceDisplacement_tilde, ns, self.mpiComm, 0)
                    solidInterfaceDisplacement_tilde1_X_Gat, solidInterfaceDisplacement_tilde1_Y_Gat, solidInterfaceDisplacement_tilde1_Z_Gat = mpiGatherInterfaceData(solidInterfaceDisplacement_tilde1, ns, self.mpiComm, 0)
                    
                    if self.myid == 0:
                        if self.FSIIter > 0: # Either information from previous time steps is re-used or not, Vk and Wk matrices are enriched only starting from the second iteration of every FSI loop
                            if self.manager.nDim == 3:
                                delta_res = np.concatenate([res_X_Gat - solidInterfaceResidual0_X_Gat, res_Y_Gat - solidInterfaceResidual0_Y_Gat, res_Z_Gat - solidInterfaceResidual0_Z_Gat], axis=0)
                                delta_d = np.concatenate([solidInterfaceDisplacement_tilde_X_Gat - solidInterfaceDisplacement_tilde1_X_Gat, solidInterfaceDisplacement_tilde_Y_Gat - solidInterfaceDisplacement_tilde1_Y_Gat, solidInterfaceDisplacement_tilde_Z_Gat - solidInterfaceDisplacement_tilde1_Z_Gat], axis = 0)
                            else:
                                delta_res = np.concatenate([res_X_Gat - solidInterfaceResidual0_X_Gat, res_Y_Gat - solidInterfaceResidual0_Y_Gat], axis=0)
                                delta_d = np.concatenate([solidInterfaceDisplacement_tilde_X_Gat - solidInterfaceDisplacement_tilde1_X_Gat, solidInterfaceDisplacement_tilde_Y_Gat - solidInterfaceDisplacement_tilde1_Y_Gat], axis = 0)
                            
                            Vk.insert(0, delta_res)
                            Wk.insert(0, delta_d)
                            
                            nIt+=1
                        
                        Vk_mat = np.vstack(Vk).T
                        Wk_mat = np.vstack(Wk).T
                        
                        if (Vk_mat.shape[1] > self.manager.nDim*ns and self.qrFilter == 'Degroote1'): # Remove extra columns if number of iterations (i.e. columns of Vk and Wk) is larger than number of interface degrees of freedom 
                            mpiPrint('WARNING: IQN-ILS Algorithm using \'Degroote1\' QR filter. Approximated stiffness matrix number of columns exceeds the number of degrees of freedom at FSI interface. Extra columns (the oldest ones!) are deleted for next iterations to avoid overdetermined problem!', self.mpiComm)
                            Vk_mat = np.delete(Vk_mat, np.s_[(self.manager.nDim*ns-Vk_mat.shape[1]):], 1)
                            Wk_mat = np.delete(Wk_mat, np.s_[(self.manager.nDim*ns-Wk_mat.shape[1]):], 1)
                        
                        dummy_V = Vk_mat.copy()
                        dummy_W = Wk_mat.copy()
                        
                        if self.manager.nDim == 3:
                            dummy_Res = np.concatenate([res_X_Gat, res_Y_Gat, res_Z_Gat], axis=0)
                        else:
                            dummy_Res = np.concatenate([res_X_Gat, res_Y_Gat], axis=0)
                        
                        if self.useQR: # Technique described by Degroote et al.
                            c, dummy_W = self.qrSolve(dummy_V, dummy_W, dummy_Res)
                        else:
                            c = np.linalg.lstsq(dummy_V, -dummy_Res)[0] # Classical QR decomposition: NOT RECOMMENDED!
                        
                        if self.manager.nDim == 3:
                            delta_ds_loc = np.split((np.dot(dummy_W,c).T + np.concatenate([res_X_Gat, res_Y_Gat, res_Z_Gat], axis=0)),3,axis=0)
                            
                            delta_ds_loc_X = delta_ds_loc[0]
                            delta_ds_loc_Y = delta_ds_loc[1]
                            delta_ds_loc_Z = delta_ds_loc[2]
                        else:
                            delta_ds_loc = np.split((np.dot(dummy_W,c).T + np.concatenate([res_X_Gat, res_Y_Gat], axis=0)),2,axis=0)
                            
                            delta_ds_loc_X = delta_ds_loc[0]
                            delta_ds_loc_Y = delta_ds_loc[1]
                            delta_ds_loc_Z = np.zeros(ns)
                        
                        for iVertex in range(delta_ds_loc_X.shape[0]):
                            iGlobalVertex = self.manager.getGlobalIndex('solid', self.myid, iVertex)
                            delta_ds[iGlobalVertex] = [delta_ds_loc_X[iVertex], delta_ds_loc_Y[iVertex], delta_ds_loc_Z[iVertex]]
                    
                    # --- Go back to parallel run --- #
                    mpiBarrier(self.mpiComm)
                    delta_ds.assemble()
                    self.interfaceInterpolator.solidInterfaceDisplacement += delta_ds
                
                if self.computeTangentMatrixBasedOnFirstIt:
                    if self.FSIIter == 0:
                        res.copy(solidInterfaceResidual0)
                        solidInterfaceDisplacement_tilde.copy(solidInterfaceDisplacement_tilde1)
                else:
                    res.copy(solidInterfaceResidual0)
                    solidInterfaceDisplacement_tilde.copy(solidInterfaceDisplacement_tilde1)
            
            if self.writeInFSIloop == True:
                self.writeRealTimeData()
            
            self.FSIIter += 1
        
        # if comm.myself == rootProcess
        
        # update of the matrices V and W at the end of the while
        if self.nbTimeToKeep != 0 and self.timeIter >= 1:
            
            # --- Trick to avoid breaking down of the simulation in the rare cases when, in the initial time steps, FSI convergence is reached without iterating (e.g. starting from a steady condition and using very small time steps), leading to empty V and W matrices ---
            if not (self.FSIIter == 1 and self.FSIConv and len(self.V)==0):
                
                self.convergenceReachedInOneIt = False
                
                # --- Managing situations where FSI convergence is not reached ---
                if (self.FSIIter >= nbFSIIter and not self.FSIConv):
                    mpiPrint('WARNING: IQN-ILS using information from {} previous time steps reached max number of iterations. Next time step is run without using any information from previous time steps!'.format(self.nbTimeToKeep), self.mpiComm)
                    
                    self.maxNbOfItReached = True
                    self.V = []
                    self.W = []
                else:
                    self.maxNbOfItReached = False
                    
                    mpiPrint('\nUpdating V and W matrices...\n', self.mpiComm)
                    
                    self.V.insert(0, Vk_mat[:,0:nIt].T)
                    self.W.insert(0, Wk_mat[:,0:nIt].T)
                    
                    if (self.timeIter > self.nbTimeToKeep and len(self.V) > self.nbTimeToKeep):
                        del self.V[-1]
                        del self.W[-1]
                # --- 
            else:
                mpiPrint('\nWARNING: IQN-ILS algorithm convergence reached in one iteration at the beginning of the simulation. V and W matrices cannot be built. BGS will be employed for the next time step!\n', self.mpiComm)
                self.convergenceReachedInOneIt = True
            # ---
            
        # --- Update the FSI history file --- #
        if self.timeIter > self.timeIterTreshold:
            mpiPrint('\n*************** IQN-ILS is converged ***************', self.mpiComm)

class ThermalAlgorithmBGS(AlgorithmBGSStaticRelax):
    """
    Des.
    """

    def __init__(self,Manager, FluidSolver, SolidSolver, InterfaceInterpolator, Criterion, nbFSIIterMax, deltaT, totTime, timeIterTreshold=-1, mpiComm=None):
        """
        Des.
        """

        AlgorithmBGSStaticRelax.__init__(self, Manager, FluidSolver, SolidSolver, InterfaceInterpolator, Criterion, nbFSIIterMax, deltaT, totTime, timeIterTreshold, 1.0, mpiComm)

    def setFSIInitialConditions(self):
        """
        Des.
        """

        if self.interfaceInterpolator.chtTransferMethod == 'hFFB' or self.interfaceInterpolator.chtTransferMethod == 'TFFB':
            #self.interfaceInterpolator.getHeatFluxFromSolidSolver()
            #self.interfaceInterpolator.interpolateSolidHeatFluxOnFluidMesh()
            #self.interfaceInterpolator.setHeatFluxToFluidSolver(self.time) # Modified by M.L. Cerquaglia but not sure if this line is still useful at all
            self.FluidSolver.setInitialInterfaceHeatFlux()
        elif self.interfaceInterpolator.chtTransferMethod == 'hFTB' or self.interfaceInterpolator.chtTransferMethod == 'FFTB':
            self.FluidSolver.setInitialInterfaceTemperature()

    def fsiCoupling(self):
        """
        Block Gauss Seidel (BGS) method for strong coupling FSI
        """

        if self.timeIter > self.timeIterTreshold:
            nbFSIIter = self.nbFSIIterMax
            mpiPrint('\n*************** Enter Block Gauss Seidel (BGS) method for strong coupling CHT ***************', self.mpiComm)
        else:
             nbFSIIter = 1

        self.FSIIter = 0
        self.FSIConv = False
        self.errValue = 0.0
        self.errValue_CHT = 1e12

        solidHasRun = False

        while ((self.FSIIter < nbFSIIter) and (not self.criterion.isVerified(self.errValue, self.errValue_CHT))):
            mpiPrint("\n>>>> FSI iteration {} <<<<\n".format(self.FSIIter), self.mpiComm)

            # --- Solid to fluid thermal transfer --- #
            if solidHasRun:
                self.solidToFluidThermalTransfer()

            # --- Fluid solver call for FSI subiteration --- #
            mpiPrint('\nLaunching fluid solver...', self.mpiComm)
            self.fluidSolverTimer.start()
            self.FluidSolver.run(self.time-self.deltaT, self.time)
            self.fluidSolverTimer.stop()
            self.fluidSolverTimer.cumul()
            mpiBarrier(self.mpiComm)

            if self.timeIter > self.timeIterTreshold:
                # --- Fluid to solid thermal transfer --- #
                self.fluidToSolidThermalTransfer()
                mpiBarrier(self.mpiComm)

                # --- Solid solver call for FSI subiteration --- #
                mpiPrint('\nLaunching solid solver...\n', self.mpiComm)
                if self.myid in self.manager.getSolidSolverProcessors():
                    self.solidSolverTimer.start()
                    self.SolidSolver.run(self.time-self.deltaT, self.time)
                    self.solidSolverTimer.stop()
                    self.solidSolverTimer.cumul()
                solidHasRun = True

                # --- Compute the thermal residual --- #
                res_CHT = self.computeSolidInterfaceResidual_CHT()
                self.errValue_CHT = self.criterion.updateHeatFlux(res_CHT)
                mpiPrint('\nCHT error value : {}\n'.format(self.errValue_CHT), self.mpiComm)
                # --- Monitor the coupling convergence --- #
                self.FSIConv = self.criterion.isVerified(self.errValue, self.errValue_CHT)

                # --- Relaxe the solid thermal data --- #
                self.relaxCHT()

            if self.writeInFSIloop == True:
                self.writeRealTimeData()

            self.FSIIter += 1
            if self.manager.computationType != 'unsteady':
                self.time += self.deltaT

            # --- Update the solvers for the next BGS iteration --- #
            if self.myid in self.manager.getSolidSolverProcessors():
                self.SolidSolver.bgsUpdate()
            self.FluidSolver.bgsUpdate()

        if self.timeIter > self.timeIterTreshold:
            mpiPrint('\n*************** BGS is converged ***************', self.mpiComm)

# --- Solid test algorithm ---
class FsiSolidTestAlgorithm:
    def __init__(self, _solid):
        self.solid = _solid

    def run(self):
        # --------------------------
        # fake FSI solver
        # --------------------------

        t1 = 0.0  # initial time
        dt = 0.5  # time step size
        nt = 10

        # we want nt time steps
        for j in range(nt):

            # each time step is arbitrarily calculated twice (for testing purpose)
            for i in range(2):

                t2=t1+dt  # time to be calculated

                self.solid.fakeFluidSolver(t2)  # creates some dummy loads for time=t2

                # run solid solver
                print '='*80
                print "running from %f to %f: try #%d" % (t1,t2,i+1)
                print '='*80
                self.solid.run(t1,t2)

                # gets the deformed interface
                dx, dy, dz = self.solid.getNodalDisplacements()
                print dx
                print dy
                print dz

            self.solid.update()
            self.solid.save()

            t1=t2 # fsi loop has converged - time t2 is accepted

        # end.
