
# Version 3.0 14/09/20
# Authors: Stephen Marsland, Nirosha Priyadarshani, Julius Juodakis, Virginia Listanti

# This is the processing class for the batch AviaNZ interface

#    AviaNZ bioacoustic analysis program
#    Copyright (C) 2017--2020

#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.

#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.

#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
import gc, os, re, fnmatch

import numpy as np

import SignalProc
import Segment
import WaveletSegment
import SupportClasses
import wavio

import traceback
import time

import math
import copy


class AviaNZ_batchProcess():
    # Main class for batch processing
    # Contains the algorithms, not the GUI, so that it can be run from commandline
    # Also called by the GUI
    # Parent: AviaNZ_batchWindow
    # mode: "GUI/CLI/test". If GUI, must provide the parent
    def __init__(self, parent, mode="GUI", configdir='', sdir='', recogniser=None, wind=0, maxgap=1.0, minlen=0.5, maxlen=10.0):
        # read config and filters from user location
        # recogniser - filter file name without ".txt"
        self.configdir = configdir
        self.configfile = os.path.join(configdir, "AviaNZconfig.txt")
        self.ConfigLoader = SupportClasses.ConfigLoader()
        self.config = self.ConfigLoader.config(self.configfile)

        self.filtersDir = os.path.join(configdir, self.config['FiltersDir'])
        self.FilterDicts = self.ConfigLoader.filters(self.filtersDir)

        if mode=="GUI":
            self.CLI = False
            self.testmode = False
            if parent is None:
                print("ERROR: must provide a parent UI or specify CLI/testmode")
                return
            self.ui = parent
        elif mode=="CLI":
            self.CLI = True
            self.testmode = False
        elif mode=="test":
            self.CLI = False
            self.testmode = True
        elif mode=="export":
            self.CLI = False
            self.testmode=False
        else:
            print("ERROR: unrecognized mode ", mode)
            return

        self.dirName = sdir
        self.wind = wind

        # Parameters for "Any sound" post-proc:
        self.maxgap = maxgap
        self.minlen = minlen
        self.maxlen = maxlen

        # In CLI/test modes, immediately run detection on init.
        # Otherwise GUI will ping that once it is moved to the right thread
        if self.CLI or self.testmode:
            self.species = [recogniser]
            self.detect()
        else:
            self.species = recogniser

    # from memory_profiler import profile
    # fp = open('memory_profiler_batch.log', 'w+')
    # @profile(stream=fp)
    def detect(self):
        # This is the function that does the work.
        # Chooses the filters and sampling regime to use.
        # Then works through the directory list, and processes each file.

        # REQUIRES: species, dirName, and wind must be set on self

        if "Any sound" in self.species:
            self.method = "Default"
            speciesStr = "Any sound"
            filters = None
        elif "Any sound (Intermittent sampling)" in self.species:
            self.method = "Intermittent sampling"
            speciesStr = "Intermittent sampling"
            filters = None
        else:
            if "NZ Bats" in self.species:
                # Should bats only be possible alone?
                self.method = "Click"   # old bat method
                #self.CNNDicts = self.ConfigLoader.CNNmodels(self.FilterDicts, self.filtersDir, self.species)
            elif "NZ Bats_NP" in self.species:
                self.method = "Bats"
            else:
                self.method = "Wavelets"

            # double-check that all Fs are equal (should already be prevented by UI)
            filters = [self.FilterDicts[name] for name in self.species]
            samplerate = set([filt["SampleRate"] for filt in filters])
            if len(samplerate)>1:
                raise ValueError("ERROR: multiple sample rates found in selected recognisers, change selection")

            # convert list to string
            speciesStr = " & ".join(self.species)

            # load target CNN models (currently stored in the same dir as filters)
            # format: {filtername: [model, win, inputdim, output]}
            self.CNNDicts = self.ConfigLoader.CNNmodels(self.FilterDicts, self.filtersDir, self.species)

        # LIST ALL FILES that will be processed (either wav or bmp, depending on mode)
        allwavs = []
        for root, dirs, files in os.walk(str(self.dirName)):
            for filename in files:
                if (self.method != "Click" and filename.lower().endswith('.wav')) or (self.method == "Click" and filename.lower().endswith('.bmp')) or (self.method == "Bats" and filename.lower().endswith('.bmp')):
                    allwavs.append(os.path.join(root, filename))
        total = len(allwavs)

        # Parse the user-set time window to process
        if self.CLI or self.testmode:
            timeWindow_s = 0
            timeWindow_e = 0
        else:
            timeWindow_s = self.ui.w_timeStart.time().hour() * 3600 + self.ui.w_timeStart.time().minute() * 60 + self.ui.w_timeStart.time().second()
            timeWindow_e = self.ui.w_timeEnd.time().hour() * 3600 + self.ui.w_timeEnd.time().minute() * 60 + self.ui.w_timeEnd.time().second()

        # LOG FILE is read here
        # note: important to log all analysis settings here
        self.filesDone = []
        if not self.testmode:
            if self.method != "Intermittent sampling":
                settings = [self.method, timeWindow_s, timeWindow_e, self.wind]
            else:
                settings = [self.method, timeWindow_s, timeWindow_e, self.config["protocolSize"], self.config["protocolInterval"]]
            print(self.method, settings)
            self.log = SupportClasses.Log(os.path.join(self.dirName, 'LastAnalysisLog.txt'), speciesStr, settings)

            # Ask for RESUME CONFIRMATION here
            if self.log.possibleAppend:
                filesExistAndDone = self.log.getDoneFiles(allwavs)
                text = "Previous analysis found in this folder (analysed " + str(len(filesExistAndDone)) + " out of " + str(total) + " files in this folder).\nWould you like to resume that analysis?"
                if not self.CLI:
                    # this is super noodly but it assumes that self.CLI always means
                    # that this class was extended with the Qt-specific things.
                    self.mutex.lock()
                    self.need_msg.emit("Resume previous batch analysis?", text)
                    self.ui.msgClosed.wait(self.mutex)
                    self.mutex.unlock()
                    confirmedResume = self.ui.msg_response

                    if confirmedResume==0:
                        self.filesDone = filesExistAndDone
                    elif confirmedResume==1:
                        self.filesDone = []
                    else:  # (cancel/Esc)
                        print("Analysis cancelled")
                        raise GentleExitException
                else:
                    confirmedResume = input(text)
                    if confirmedResume.lower() == 'yes' or confirmedResume.lower() == 'y':
                        # ignore files in log
                        self.filesDone = filesExistAndDone
                    else:
                        # process all files
                        self.filesDone = []
                #if len(filesExistAndDone) == total:
                    # TODO: might want to redo?
                    #print("All files appear to have previous analysis results")
                    #return
            else:
                # work on all files
                self.filesDone = []

            # Ask for FINAL USER CONFIRMATION here
            cnt = len(self.filesDone)
            if self.method == "Intermittent sampling":
                text = "Method: " + self.method + ".\nNumber of files to analyse: " + str(total) + "\n"
            else:
                text = "Species: " + speciesStr + ", method: " + self.method + ".\nNumber of files to analyse: " + str(total) + ", " + str(cnt) + " done so far.\n"

            text += "Log file stored in " + self.dirName + "/LastAnalysisLog.txt.\n"
            if speciesStr == "Any sound" or self.method == "Click" or self.method == "Bats":
                text += "\nWarning: any previous annotations in these files will be deleted!\n"
            else:
                text += "\nWarning: any previous annotations for the selected species in these files will be deleted!\n"

            text = "Analysis will be launched with these settings:\n" + text + "\nConfirm?"

            if not self.CLI:
                self.mutex.lock()
                self.need_msg.emit("Launch batch analysis",text)
                self.ui.msgClosed.wait(self.mutex)
                self.mutex.unlock()
                confirmedLaunch = self.ui.msg_response==0
            else:
                confirmedLaunch = input(text+"[y/n]")
                print(confirmedLaunch.lower(),)
                if confirmedLaunch.lower() == 'yes' or confirmedLaunch.lower() == 'y':
                    confirmedLaunch = True
                else:
                    confirmedLaunch = False

            if not confirmedLaunch:
                print("Analysis cancelled")
                raise GentleExitException

            # update log: delete everything (by opening in overwrite mode),
            # reprint old headers,
            # print current header (or old if resuming),
            # print old file list if resuming.
            self.log.file = open(self.log.filepath, 'w')
            if speciesStr not in ["Any sound", "Intermittent sampling"]:
                self.log.reprintOld()
                # else single-sp runs should be deleted anyway

            self.log.appendHeader(header=None, species=self.log.species, settings=self.log.settings)
        else:
            settings = [self.method, timeWindow_s, timeWindow_e, self.wind]

        if not self.CLI and not self.testmode:
            # clean up the UI before entering the long loop
            # and wait to confirm that all the dialogs are in place
            self.mutex.lock()
            self.need_clean_UI.emit(total, cnt)
            self.ui.msgClosed.wait(self.mutex)
            self.mutex.unlock()

            import pyqtgraph as pg
            with pg.BusyCursor():
                self.mainloop(allwavs,total,speciesStr,filters,settings)
        else:
            self.mainloop(allwavs,total,speciesStr,filters,settings)

        if not self.testmode:
            # delete old results (xlsx)
            # ! WARNING: any Detection...xlsx files will be DELETED,
            # ! ANYWHERE INSIDE the specified dir, recursively

            # We currently do not export any excels automatically in this mode,
            # the user must do that manually (through Batch Review).

            print("Removing old Excel files...")
            if not self.CLI:
                self.need_update.emit(total,"Removing old Excel files, almost done...")

            for root, dirs, files in os.walk(str(self.dirName)):
                for filename in files:
                    filenamef = os.path.join(root, filename)
                    if fnmatch.fnmatch(filenamef, '*DetectionSummary_*.xlsx'):
                        print("Removing excel file %s" % filenamef)
                        os.remove(filenamef)

            # At the end, if processing bats, export BatSearch xml automatically and check if want to export DOC database (in CLI mode, do it automatically, with missing data!)
            if self.method == 'Click':
                self.exportToBatSearch(self.dirName)
                self.outputBatPasses(self.dirName)
            elif self.method == 'Bats':
                self.exportToBatSearch(self.dirName,threshold1=100,threshold2=None)
                self.outputBatPasses(self.dirName)
                #self.exportToBatSearch_NP(self.dirName)

            if self.method=='Click' or self.method=='Bats':
                if not self.CLI:
                    # TODO: what if you start from a different folder?
                    # I think that this is OK, but need to check -- it should (?) put a BatDB file in each folder, just like the log files.
                    # Then it's up to the user to sort them. Or maybe not?
                    # TODO: autofill some metadata if user has filled it in once?
                    easting = ""
                    northing = ""
                    try:
                        f = open(os.path.join(self.dirName,'log.txt'),'r')
                        # Find a line that contains GPS (lat, long),
                        # And read the two numbers after it
                        # This version just returns the last ones
                        for line in f.readlines():
                            if 'GPS (lat,long)' in line:
                                ll = line.strip()
                                y = ll.split(",")
                                x = ll[-2].split(":")
                                easting = x[-1]
                                northing = y[-1]
                            elif 'GPS:' in line:
                                ll = line.strip()
                                y = ll.split("=")
                                x = y[-2].split(",")
                                easting = x[-2]
                                northing = y[-1]
                    except FileNotFoundError:
                        pass
                    except Exception as e:
                        print("Warning: could not read GPS data, ", e)

                    recorder = os.path.split(self.dirName)[-1]

                    # ping UI to show the survey form
                    self.mutex.lock()
                    self.need_bat_info.emit(self.config['operator'],easting,northing,recorder)
                    self.ui.msgClosed.wait(self.mutex)
                    self.mutex.unlock()

                    # now, the form was either rejected, setting results to None,
                    # or accepted:
                    if self.ui.batFormResults is not None:
                        self.exportBatSurvey(self.dirName, self.ui.batFormResults)
                else:
                    self.exportBatSurvey(self.dirName, None)

            # END of processing and exporting. Final cleanup
            self.log.file.close()

        print("Processed all %d files" % total)
        return(0)

    def mainloop(self,allwavs,total,speciesStr,filters,settings):
        # MAIN PROCESSING starts here
        processingTime = 0
        cleanexit = 0
        cnt = 0

        timeWindow_s = settings[1]
        timeWindow_e = settings[2]

        for filename in allwavs:
            # get remaining run time in min
            processingTimeStart = time.time()
            hh,mm = divmod(processingTime * (total-cnt) / 60, 60)
            cnt = cnt+1
            progrtext = "file %d / %d. Time remaining: %d h %.2f min" % (cnt, total, hh, mm)

            print("*** Processing" + progrtext + " ***")

            # if it was processed previously (stored in log)
            if filename in self.filesDone:
                # skip the processing:
                print("File %s processed previously, skipping" % filename)
                self.log.appendFile(filename)
                continue

            # check if file not empty
            if os.stat(filename).st_size < 1000:
                print("File %s empty, skipping" % filename)
                if not self.testmode:
                    self.log.appendFile(filename)
                continue

            # check if file is formatted correctly
            with open(filename, 'br') as f:
                if (self.method == "Click" and f.read(2) != b'BM') or (self.method == "Bats" and f.read(2) != b'BM') or (self.method != "Click" and self.method != "Bats" and f.read(4) != b'RIFF'):
                    print("Warning: file %s not formatted correctly, skipping" % filename)
                    self.log.appendFile(filename)
                    continue

            # test the selected time window if it is a doc recording
            DOCRecording = re.search(r'(\d{6})_(\d{6})', os.path.basename(filename))
            if DOCRecording:
                startTime = DOCRecording.group(2)
                sTime = int(startTime[:2]) * 3600 + int(startTime[2:4]) * 60 + int(startTime[4:6])
                if timeWindow_s == timeWindow_e:
                    # (no time window set)
                    inWindow = True
                elif timeWindow_s < timeWindow_e:
                    # for day times ("8 to 17")
                    inWindow = (sTime >= timeWindow_s and sTime <= timeWindow_e)
                else:
                    # for times that include midnight ("17 to 8")
                    inWindow = (sTime >= timeWindow_s or sTime <= timeWindow_e)
            else:
                inWindow = True

            if DOCRecording and not inWindow:
                print("Skipping out-of-time-window recording")
                if not self.testmode:
                    self.log.appendFile(filename)
                continue

            # ALL SYSTEMS GO: process this file
            self.filename = filename
            self.segments = Segment.SegmentList()
            if self.testmode:
                self.segments_nocnn = Segment.SegmentList()
            if self.method == "Intermittent sampling":
                try:
                    self.addRegularSegments()
                except Exception:
                    estr = "Encountered error:\n" + traceback.format_exc()
                    print("ERROR: ", estr)
                    self.log.file.close()
                    raise
            else:
                # load audiodata/spectrogram and clean up old segments:
                print("Loading file...")
                # Impulse masking:   TODO masking is useful but could be improved
                if speciesStr=="Any sound":
                    impMask = True  # Up to debate - could turn this off here
                elif self.method=="Click" or self.method=="Bats":
                    impMask = False  # definitely off for bats
                else:
                    # MUST BE off for changepoints (it introduces discontinuities, which
                    # create large WCs and highly distort means/variances)
                    impMask = "chp" not in [sf.get("method") for sf in filters]
                self.loadFile(species=self.species, anysound=(speciesStr == "Any sound"), impMask=impMask)

                # initialize empty segmenter
                if self.method=="Wavelets":
                    self.ws = WaveletSegment.WaveletSegment(wavelet='dmey2')
                    del self.sp
                    gc.collect()

                # Main work is done here:
                try:
                    print("Segmenting...")
                    self.detectFile(speciesStr, filters)
                except GentleExitException:
                    raise
                except Exception:
                    estr = "Encountered error:\n" + traceback.format_exc()
                    print("ERROR: ", estr)
                    self.log.file.close()
                    raise

                print('Segments in this file: ', self.segments)

            # export segments
            print("%d new segments marked" % len(self.segments))
            if self.testmode:
                # save separately With and without CNN
                cleanexit = self.saveAnnotation(self.segments, suffix=".tmpdata")
                cleanexit = self.saveAnnotation(self.segments_nocnn, suffix=".tmp2data")
            else:
                cleanexit = self.saveAnnotation(self.segments)
            if cleanexit != 1:
                print("Warning: could not save segments!")

            # Log success for this file and update ProgrDlg
            if not self.testmode:
                self.log.appendFile(filename)
                if not self.CLI:
                    self.need_update.emit(cnt,"Analysed "+progrtext)
                    # TODO sprinkle more of these checks
                    if self.ui.dlg.wasCanceled():
                        print("Analysis cancelled")
                        self.log.file.close()
                        raise GentleExitException
            # track how long it took to process one file:
            processingTime = time.time() - processingTimeStart
            print("File processed in", processingTime)
            # END of audio batch processing

    def addRegularSegments(self):
        """ Perform the Hartley bodge: add 10s segments every minute. """
        # if wav.data exists get the duration
        (rate, nseconds, nchannels, sampwidth) = wavio.readFmt(self.filename)
        self.segments.metadata = dict()
        self.segments.metadata["Operator"] = "Auto"
        self.segments.metadata["Reviewer"] = ""
        self.segments.metadata["Duration"] = nseconds
        i = 0
        segments = []
        print("Adding segments (%d s every %d s) to %s" %(self.config['protocolSize'], self.config['protocolInterval'], str(self.filename)))
        while i < nseconds:
            segments.append([i, i + self.config['protocolSize']])
            i += self.config['protocolInterval']
        post = Segment.PostProcess(configdir=self.configdir, audioData=None, sampleRate=0, segments=segments, subfilter={}, cert=0)
        self.makeSegments(self.segments, post.segments)

    def detectFile(self, speciesStr, filters):
        """ Actual worker for a file in the detection loop.
            Does not return anything - for use with external try/catch
        """
        # Segment over pages separately, to allow dealing with large files smoothly:
        # (page size is shorter for low freq things, i.e. bittern,
        # since those freqs are very noisy and variable)
        if self.sampleRate<=4000:
            # Basically bittern
            samplesInPage = 300*self.sampleRate
        elif self.method=="Wavelets":
            # If using changepoints and v short windows,
            # aim to have roughly 5000 windows:
            # (4500 = 4 windows in 15 min DoC standard files)
            winsize = [subf["WaveletParams"].get("win", 1) for f in filters for subf in f["Filters"]]
            winsize = min(winsize)
            if winsize<0.05:
                samplesInPage = int(4500 * 0.05 * self.sampleRate)
            else:
                samplesInPage = 900*16000
        else:
            # A sensible default
            samplesInPage = 900*16000

        # (ceil division for large integers)
        numPages = (self.datalength - 1) // samplesInPage + 1

        # Actual segmentation happens here:
        for page in range(numPages):
            print("Segmenting page %d / %d" % (page+1, numPages))
            start = page*samplesInPage
            end = min(start+samplesInPage, self.datalength)
            thisPageLen = (end-start) / self.sampleRate

            if thisPageLen < 2 and (self.method != "Click" and self.method != "Bats"):
                print("Warning: can't process short file ends (%.2f s)" % thisPageLen)
                continue

            # Process
            if speciesStr == "Any sound":
                # Create spectrogram for median clipping etc
                if not hasattr(self, 'sp'):
                    self.sp = SignalProc.SignalProc(self.config['window_width'], self.config['incr'])
                self.sp.data = self.audiodata[start:end]
                self.sp.sampleRate = self.sampleRate
                _ = self.sp.spectrogram(window='Hann', sgType='Standard', mean_normalise=True, onesided=True)
                self.seg = Segment.Segmenter(self.sp, self.sampleRate)
                # thisPageSegs = self.seg.bestSegments()
                thisPageSegs = self.seg.medianClip(thr=3.5)
                # Post-process
                print("Segments detected: ", len(thisPageSegs))
                print("Post-processing...")
                post = Segment.PostProcess(configdir=self.configdir, audioData=self.audiodata[start:end], sampleRate=self.sampleRate, segments=thisPageSegs, subfilter={}, cert=0)
                post.joinGaps(self.maxgap)
                post.deleteShort(self.minlen)
                # avoid extra long segments (for Isabel)
                post.splitLong(self.maxlen)

                # adjust segment starts for 15min "pages"
                if start != 0:
                    for seg in post.segments:
                        seg[0][0] += start/self.sampleRate
                        seg[0][1] += start/self.sampleRate
                # attach mandatory "Don't Know"s etc and put on self.segments
                self.makeSegments(self.segments, post.segments)
                del self.seg
                gc.collect()
                # After each page, check for interrupts:
                if not self.CLI:
                    if self.ui.dlg.wasCanceled():
                        print("Analysis cancelled")
                        self.log.file.close()
                        raise GentleExitException
            else:
                if self.method != "Click" and self.method != "Bats":
                    # read in the page and resample as needed
                    self.ws.readBatch(self.audiodata[start:end], self.sampleRate, d=False, spInfo=filters, wpmode="new", wind=self.wind>0)

                data_test = []
                click_label = 'None'
                for speciesix in range(len(filters)):
                    print("Working with recogniser:", filters[speciesix])
                    if self.method=="Click":
                        click_label, data_test, gen_spec = self.ClickSearch(self.sp.sg, self.filename)
                        print('number of detected clicks = ', gen_spec)
                        thisPageSegs = []
                    elif self.method == "Bats":
                        thisPageSegs = []   # No click search
                    else:
                        # Bird detection by wavelets. Choose the right wavelet method:
                        if "method" not in filters[speciesix] or filters[speciesix]["method"]=="wv":
                            # note: using 'recaa' mode = partial antialias
                            thisPageSegs = self.ws.waveletSegment(speciesix, wpmode="new")
                        elif filters[speciesix]["method"]=="chp":
                            # note that only allowing alg2 = nuisance-robust chp detection
                            thisPageSegs = self.ws.waveletSegmentChp(speciesix, alg=2, wind=self.wind)
                        else:
                            print("ERROR: unrecognized method", filters[speciesix]["method"])
                            raise Exception

                    # Post-process:
                    # CNN-classify, delete windy, rainy segments, check for FundFreq, merge gaps etc.
                    print("Segments detected (all subfilters): ", thisPageSegs)
                    if not self.testmode and self.method != "Bats":
                        print("Post-processing...")
                    # postProcess currently operates on single-level list of segments,
                    # so we run it over subfilters for wavelets:
                    spInfo = filters[speciesix]
                    for filtix in range(len(spInfo['Filters'])):
                        CNNmodel = None
                        if 'CNN' in spInfo:
                            if spInfo['CNN']['CNN_name'] in self.CNNDicts.keys():
                                # This list contains the model itself, plus parameters for running it
                                CNNmodel = self.CNNDicts[spInfo['CNN']['CNN_name']]

                        if not self.testmode:
                            # TODO THIS IS FULL POST-PROC PIPELINE FOR BIRDS AND BATS
                            # -- Need to check how this should interact with the testmode

                            if self.method=="Click":
                                # bat-style CNN:
                                model = CNNmodel[0]
                                thr1 = CNNmodel[5][0]
                                thr2 = CNNmodel[5][1]
                                if click_label=='Click':
                                    # we enter in the cnn only if we got a click
                                    sg_test = np.ndarray(shape=(np.shape(data_test)[0],np.shape(data_test[0][0])[0], np.shape(data_test[0][0])[1]), dtype=float)
                                    spec_id=[]
                                    print('Number of file spectrograms = ', np.shape(data_test)[0])
                                    for j in range(np.shape(data_test)[0]):
                                        maxg = np.max(data_test[j][0][:])
                                        sg_test[j][:] = data_test[j][0][:]/maxg
                                        spec_id.append(data_test[j][1:3])

                                    # CNN classification of clicks
                                    x_test = sg_test
                                    test_images = x_test.reshape(x_test.shape[0],6, 512, 1)
                                    test_images = test_images.astype('float32')

                                    # recovering labels
                                    predictions = model.predict(test_images)
                                    # predictions is an array #imagesX #of classes which entries are the probabilities for each class

                                    # Create a label (list of dicts with species, certs) for the single segment
                                    print('Assessing file label...')
                                    label = self.File_label(predictions, thr1=thr1, thr2=thr2)
                                    print('CNN detected: ', label)
                                    if len(label)>0:
                                        # Convert the annotation into a full segment in self.segments
                                        thisPageStart = start / self.sampleRate
                                        self.makeSegments(self.segments, [thisPageStart, thisPageLen, label])
                                else:
                                    # do not create any segments
                                    print("Nothing detected")
                            elif self.method == "Bats":     # Let's do it here - PostProc class is not supporting bats
                                # TODO review this a bit - my code checker shows errors
                                model = CNNmodel[0]
                                if thisPageLen < CNNmodel[1][0]:
                                    continue
                                elif thisPageLen >= CNNmodel[1][0]:
                                    # print('duration:', thisPageLen)
                                    n = math.ceil((thisPageLen - 0 - CNNmodel[1][0]) / CNNmodel[1][1] + 1)
                                # print('* hop:', CNNmodel[1][1], 'n:', n)

                                featuress = []
                                specFrameSize = len(range(0, int(CNNmodel[1][0] * self.sp.sampleRate - self.sp.window_width), self.sp.incr))
                                for i in range(int(n)):
                                    # print('**', self.filename, CNNmodel[1][0], 0 + CNNmodel[1][1] * i, self.sp.sampleRate,
                                    #       '************************************')
                                    # Sgram images
                                    sgRaw = self.sp.sg
                                    sgstart = int(CNNmodel[1][1] * i * self.sp.sampleRate / self.sp.incr)
                                    sgend = sgstart + specFrameSize
                                    if sgend > np.shape(sgRaw)[0]:
                                        sgend = np.shape(sgRaw)[0]
                                        sgstart = np.shape(sgRaw)[0] - specFrameSize
                                    if sgstart < 0:
                                        continue
                                    sgRaw_i = sgRaw[sgstart:sgend, :]
                                    maxg = np.max(sgRaw_i)
                                    # Normalize and rotate
                                    featuress.append([np.rot90(sgRaw_i / maxg).tolist()])
                                featuress = np.array(featuress)
                                featuress = featuress.reshape(featuress.shape[0], CNNmodel[2][0], CNNmodel[2][1], 1)
                                featuress = featuress.astype('float32')
                                if np.shape(featuress)[0] > 0:
                                    probs = model.predict(featuress)
                                else:
                                    probs = 0
                                if isinstance(probs, int):
                                    # there is no at least one img generated from this segment, very unlikely to be a true seg.
                                    label = []
                                else:
                                    ind = [np.argsort(probs[:, i]).tolist() for i in range(np.shape(probs)[1])]

                                    if n > 4:
                                        n = 4
                                    prob = [np.mean(probs[ind[0][-n // 2:], 0]),
                                            np.mean(probs[ind[1][-n // 2:], 1]),
                                            (np.sum(probs[ind[0][-n // 2:], 2]) + np.sum(probs[ind[1][-n // 2:], 2])) / (n // 2 * 2)]
                                    print(self.filename, prob)
                                    if prob[0] >= CNNmodel[5][0][-1]:
                                        label = [{"species": "Long-tailed bat", "certainty": 100}]
                                    elif prob[1] >= CNNmodel[5][1][-1]:
                                        label = [{"species": "Short-tailed bat", "certainty": 100}]
                                    elif prob[0] >= CNNmodel[5][0][0]:
                                        label = [{"species": "Long-tailed bat", "certainty": 50}]
                                    elif prob[1] >= CNNmodel[5][1][0]:
                                        label = [{"species": "Short-tailed bat", "certainty": 50}]
                                    else:
                                        label = []
                                print('CNN detected: ', label)
                                if len(label) > 0:
                                    # Convert the annotation into a full segment in self.segments
                                    thisPageStart = start / self.sampleRate
                                    self.makeSegments(self.segments, [thisPageStart, thisPageLen, label])
                            else:
                                # bird-style CNN and other processing:
                                postsegs = self.postProcFull(thisPageSegs, spInfo, filtix, start, end, CNNmodel)
                                # attach filter info and put on self.segments:
                                self.makeSegments(self.segments, postsegs, self.species[speciesix], spInfo["species"], spInfo['Filters'][filtix])

                            # After each subfilter is done, check for interrupts:
                            if not self.CLI:
                                if self.ui.dlg.wasCanceled():
                                    print("Analysis cancelled")
                                    self.log.file.close()
                                    raise GentleExitException

                        else:
                            # THIS IS testmode. NOT ADAPTED TO BATS: assumes bird-style postproc
                            # TODO adapt to bats?

                            # test without cnn:
                            postsegs = self.postProcFull(copy.deepcopy(thisPageSegs), spInfo, filtix, start, end, CNNmodel=None)
                            # stash these segments before any CNN/postproc:
                            self.makeSegments(self.segments_nocnn, postsegs, self.species[speciesix], spInfo["species"], spInfo['Filters'][filtix])

                            # test with cnn:
                            postsegs = self.postProcFull(copy.deepcopy(thisPageSegs), spInfo, filtix, start, end, CNNmodel)
                            # attach filter info and put on self.segments:
                            self.makeSegments(self.segments, postsegs, self.species[speciesix], spInfo["species"], spInfo['Filters'][filtix])

    def postProcFull(self, segments, spInfo, filtix, start, end, CNNmodel):
        """ Full bird-style postprocessing (CNN, joinGaps...)
            segments: list of segments over calltypes
            start, end: start and end of this page, in samples
            CNNmodel: None or a CNN
        """
        subfilter = spInfo["Filters"][filtix]
        post = Segment.PostProcess(configdir=self.configdir, audioData=self.audiodata[start:end],
                            sampleRate=self.sampleRate, tgtsampleRate=spInfo["SampleRate"],
                            segments=segments[filtix], subfilter=subfilter,
                            CNNmodel=CNNmodel, cert=50)
        print("Segments detected after WF: ", len(segments[filtix]))

        if CNNmodel:
            print('Post-processing with CNN')
            post.CNN()

        # Fund freq and merging. Only do for standard wavelet filter currently:
        # (for median clipping, gap joining and some short segment cleanup was already done in WaveletSegment)
        if "method" not in spInfo or spInfo["method"]=="wv":
            if 'F0' in subfilter and 'F0Range' in subfilter and subfilter["F0"]:
                print("Checking for fundamental frequency...")
                post.fundamentalFrq()

            post.joinGaps(maxgap=subfilter['TimeRange'][3])

        # delete short segments, if requested:
        if subfilter['TimeRange'][0]>0:
            post.deleteShort(minlength=subfilter['TimeRange'][0])

        # adjust segment starts for 15min "pages"
        if start != 0:
            for seg in post.segments:
                seg[0][0] += start/self.sampleRate
                seg[0][1] += start/self.sampleRate
        print("After post-processing: ", post.segments)
        return(post.segments)

    def makeSegments(self, segmentsList, segmentsNew, filtName=None, species=None, subfilter=None):
        """ Adds segmentsNew to segmentsList """
        if self.method == "Click" or self.method == "Bats":
            # Batmode: segmentsNew should be already prepared as: [x1, x2, labels]
            y1 = 0
            y2 = 0
            if len(segmentsNew)!=3:
                print("Warning: segment format does not match bat mode")
            segment = Segment.Segment([segmentsNew[0], segmentsNew[1], y1, y2, segmentsNew[2]])
            segmentsList.addSegment(segment)
        elif subfilter is not None:
            # for wavelet segments: (same as self.species!="Any sound")
            y1 = subfilter["FreqRange"][0]
            y2 = min(subfilter["FreqRange"][1], self.sampleRate//2)
            for s in segmentsNew:
                segment = Segment.Segment([s[0][0], s[0][1], y1, y2, [{"species": species, "certainty": s[1], "filter": filtName, "calltype": subfilter["calltype"]}]])
                segmentsList.addSegment(segment)
        else:
            # for generic all-species segments:
            y1 = 0
            y2 = 0
            species = "Don't Know"
            cert = 0.0
            segmentsList.addBasicSegments(segmentsNew, [y1, y2], species=species, certainty=cert)

    def saveAnnotation(self, segmentList, suffix=".data"):
        """ Generates default batch-mode metadata,
            and saves the segmentList to a .data file.
            suffix arg can be used to export .tmpdata during testing.
        """
        if not hasattr(segmentList, "metadata"):
            segmentList.metadata = dict()
        segmentList.metadata["Operator"] = "Auto"
        segmentList.metadata["Reviewer"] = ""
        if self.method != "Intermittent sampling":
            segmentList.metadata["Duration"] = float(self.datalength)/self.sampleRate
        segmentList.metadata["noiseLevel"] = None
        segmentList.metadata["noiseTypes"] = []

        segmentList.saveJSON(str(self.filename) + suffix)
        return 1

    def loadFile(self, species, anysound=False, impMask=True):
        """ species: list of recognizer names, or ["Any sound"].
            Species names will be wiped based on these. """
        print(self.filename)
        # Create an instance of the Signal Processing class
        if not hasattr(self, 'sp'):
            self.sp = SignalProc.SignalProc(self.config['window_width'], self.config['incr'])

        # Read audiodata or spectrogram
        if self.method == "Click":  # old bat method
            self.sp.readBmp(self.filename, rotate=False)
            self.sampleRate = self.sp.sampleRate
            self.datalength = self.sp.fileLength
        elif self.method == "Bats":
            self.sp = SignalProc.SignalProc(512, 256)
            self.sp.readBmp(self.filename, rotate=True, repeat=False)
            self.sampleRate = self.sp.sampleRate
            self.datalength = self.sp.fileLength
        else:
            self.sp.readWav(self.filename)
            self.sampleRate = self.sp.sampleRate
            self.audiodata = self.sp.data

            self.datalength = np.shape(self.audiodata)[0]
            print("Read %d samples, %f s at %d Hz" % (len(self.audiodata), float(self.datalength)/self.sampleRate, self.sampleRate))

        # Read in stored segments (useful when doing multi-species)
        self.segments = Segment.SegmentList()
        if species == ["Any sound"] or not os.path.isfile(self.filename + '.data') or self.method == "Click" or self.method == "Bats":
            # Initialize default metadata values
            self.segments.metadata = dict()
            self.segments.metadata["Operator"] = "Auto"
            self.segments.metadata["Reviewer"] = ""
            self.segments.metadata["Duration"] = float(self.datalength)/self.sampleRate
            # wipe all segments:
            print("Wiping all previous segments")
            self.segments.clear()
        else:
            self.segments.parseJSON(self.filename+'.data', float(self.datalength)/self.sampleRate)
            # wipe same species:
            for sp in species:
                # shorthand for double-checking that it's not "Any Sound" etc
                if sp in self.FilterDicts:
                    spname = self.FilterDicts[sp]["species"]
                    print("Wiping species", spname)
                    oldsegs = self.segments.getSpecies(spname)
                    for i in reversed(oldsegs):
                        wipeAll = self.segments[i].wipeSpecies(spname)
                        if wipeAll:
                            del self.segments[i]
            print("%d segments loaded from .data file" % len(self.segments))

        # impulse masking (on by default)
        if impMask:
            if anysound:
                self.sp.data = self.sp.impMask(engp=70, fp=0.50)
            else:
                self.sp.data = self.sp.impMask()
            self.audiodata = self.sp.data

    def ClickSearch(self, imspec, file,virginia=True):
        """
        searches for clicks in the provided imspec, saves dataset
        returns click_label, dataset and count of detections

        The search is made on the spectrogram image that we know to be generated
        with parameters (1024,512)
        Click presence is assessed for each spectrogram column: if the mean in the
        frequency band [f0, f1] (*) is bigger than a treshold we have a click
        thr=mean(all_spec)+std(all_spec) (*)

        The clicks are discarded if longer than 0.05 sec

        Clicks are stored into featuress using updateDataset

        imspec: unrotated spectrogram (rows=time)
        file: NOTE originally was basename, now full filename

        The virginia flag is because I've (SM) added the other functionality, which just finds the first and last clicks to get the time
        """
        featuress = []
        count = 0

        df=self.sampleRate//2 /(np.shape(imspec)[0]+1)  # frequency increment
        dt=self.sp.incr/self.sampleRate  # self.sp.incr is set to 512 for bats
        # dt=0.002909090909090909
        # up_len=math.ceil(0.05/dt) #0.5 second lenth in indices divided by 11
        up_len=17
        # up_len=math.ceil((0.5/11)/dt)

        # Frequency band
        f0=24000
        index_f0=-1+math.floor(f0/df)  # lower bound needs to be rounded down
        f1=54000
        index_f1=-1+math.ceil(f1/df)  # upper bound needs to be rounded up

        # Mean in the frequency band
        mean_spec=np.mean(imspec[index_f0:index_f1,:], axis=0)

        # Threshold
        mean_spec_all=np.mean(imspec, axis=0)[2:]
        thr_spec=(np.mean(mean_spec_all)+np.std(mean_spec_all))*np.ones((np.shape(mean_spec)))

        ## clickfinder
        # check when the mean is bigger than the threshold
        # clicks is an array which elements are equal to 1 only where the sum is bigger
        # than the mean, otherwise are equal to 0
        clicks = mean_spec>thr_spec
        
        if virginia:
            clicks_indices = np.nonzero(clicks)
            # check: if I have found somenthing
            if np.shape(clicks_indices)[1]==0:
                click_label='None'
                return click_label, featuress, count
                # not saving spectrograms

            # Discarding segments too long or too short and saving spectrogram images
            click_start=clicks_indices[0][0]
            click_end=clicks_indices[0][0]
            for i in range(1,np.shape(clicks_indices)[1]):
                if clicks_indices[0][i]==click_end+1:
                    click_end=clicks_indices[0][i]
                else:
                    if click_end-click_start+1>up_len:
                        clicks[click_start:click_end+1] = False
                    else:
                        # savedataset
                        featuress, count = self.updateDataset(file, featuress, count, imspec, click_start, click_end, dt)
                    # update
                    click_start=clicks_indices[0][i]
                    click_end=clicks_indices[0][i]

            # checking last loop with end
            if click_end-click_start+1>up_len:
                clicks[click_start:click_end+1] = False
            else:
                featuress, count = self.updateDataset(file, featuress, count, imspec, click_start, click_end, dt)

            # Assigning: click label
            if np.any(clicks):
                click_label='Click'
            else:
                click_label='None'

            return click_label, featuress, count
        else:
            inds = np.where(clicks>0)[0]
            if (len(inds)) > 0:
                # Have found something, now find first that isn't too long
                flag = False
                start = inds[0]
                while flag:
                    i=1
                    while inds[i]-inds[i-1] == 1:
                        i+=1
                    end = i
                    if end-start<up_len:
                        flag=True
                    else:
                        start = inds[end+1]

                first = start

                # And last that isn't too long
                flag = False
                end = inds[-1]
                while flag:
                    i=len(inds)-1
                    while inds[i]-inds[i-1] == 1:
                        i-=1
                    start = i
                    if end-start<up_len:
                        flag=True
                    else:
                        end = inds[start-1]
                last = end
                return [first,last]
            else:
                return None

    def updateDataset(self, file_name, featuress, count, spectrogram, click_start, click_end, dt=None):
        """
        Update Dataset with current segment
        It take a piece of the spectrogram with fixed length centered in the click
        """
        win_pixel=1
        ls = np.shape(spectrogram)[1]-1
        click_center=int((click_start+click_end)/2)

        start_pixel=click_center-win_pixel
        if start_pixel<0:
            win_pixel2=win_pixel+np.abs(start_pixel)
            start_pixel=0
        else:
            win_pixel2=win_pixel

        end_pixel=click_center+win_pixel2
        if end_pixel>ls:
            start_pixel-=end_pixel-ls+1
            end_pixel=ls-1
            # this code above fails for sg less than 4 pixels wide
        sgRaw=spectrogram[:,start_pixel:end_pixel+1]  # not I am saving the spectrogram in the right dimension
        sgRaw=np.repeat(sgRaw,2,axis=1)
        sgRaw=(np.flipud(sgRaw)).T  # flipped spectrogram to make it consistent with Niro Mewthod
        featuress.append([sgRaw.tolist(), file_name, count])  # not storing segment and label informations

        count += 1

        return featuress, count

    def File_label(self, predictions, thr1, thr2):
        """
        uses the predictions made by the CNN to update the filewise annotations
        when we have 3 labels: 0 (LT), 1(ST), 2 (Noise)

        METHOD: evaluation of probability over files combining mean of probability
            + best3mean of probability against thr1 and thr2, respectively

        Returns: species labels (list of dicts), compatible w/ the label format on Segments
        """

        # Assessing file label
        # inizialization
        # vectors storing classes probabilities
        LT_prob=[]  # class 0
        ST_prob=[]  # class 1
        NT_prob=[]  # class 2
        spec_num=0   # counts number of spectrograms per file
        # flag: if no click detected no spectrograms
        click_detected_flag=False
        # looking for all the spectrogram related to this file

        for k in range(np.shape(predictions)[0]):
            click_detected_flag=True
            spec_num+=1
            LT_prob.append(predictions[k][0])
            ST_prob.append(predictions[k][1])
            NT_prob.append(predictions[k][2])

        # if no clicks => automatically Noise
        label = []

        if click_detected_flag:
            # mean
            LT_mean=np.mean(LT_prob)*100
            ST_mean=np.mean(ST_prob)*100

            # best3mean
            LT_best3mean=0
            ST_best3mean=0

            # LT
            ind = np.array(LT_prob).argsort()[-3:][::-1]
            # adding len ind in order to consider also the cases when we do not have 3 good examples
            if len(ind)==1:
                # this means that there is only one prob!
                LT_best3mean+=LT_prob[0]
            else:
                for j in range(len(ind)):
                    LT_best3mean+=LT_prob[ind[j]]
            LT_best3mean/= 3
            LT_best3mean*=100

            # ST
            ind = np.array(ST_prob).argsort()[-3:][::-1]
            # adding len ind in order to consider also the cases when we do not have 3 good examples
            if len(ind)==1:
                # this means that there is only one prob!
                ST_best3mean+=ST_prob[0]
            else:
                for j in range(len(ind)):
                    ST_best3mean+=ST_prob[ind[j]]
            ST_best3mean/= 3
            ST_best3mean*=100

            # ASSESSING FILE LABEL
            hasST = ST_mean>=thr1 or ST_best3mean>=thr2
            hasLT = LT_mean>=thr1 or LT_best3mean>=thr2
            hasSTlow = ST_mean<thr1 and ST_best3mean<thr2
            hasLTlow = LT_mean<thr1 and LT_best3mean<thr2
            reallyHasST = ST_mean>=thr1 and ST_best3mean>=thr2
            reallyHasLT = LT_mean>=thr1 and LT_best3mean>=thr2
            HasBat = LT_mean>=thr1 and ST_mean>=thr1

            if reallyHasLT and hasSTlow:
                label.append({"species": "Long-tailed bat", "certainty": 100})
            elif reallyHasLT and reallyHasST:
                label.append({"species": "Long-tailed bat", "certainty": 100})
            elif hasLT and ST_mean<thr1:
                label.append({"species": "Long-tailed bat", "certainty": 50})
            elif HasBat:
                label.append({"species": "Long-tailed bat", "certainty": 50})

            if reallyHasST and hasLTlow:
                label.append({"species": "Short-tailed bat", "certainty": 100})
            elif reallyHasLT and reallyHasST:
                label.append({"species": "Short-tailed bat", "certainty": 100})
            elif hasST and LT_mean<thr1:
                label.append({"species": "Short-tailed bat", "certainty": 50})
            elif HasBat:
                label.append({"species": "Short-tailed bat", "certainty": 50})

        return label

    def outputBatPasses(self,dirName,savefile='BatPasses.csv'):
        # A bit ad hoc for now. Assumes that the directory structure ends with 'Bat detname date/date/'
        if not hasattr(self, 'sp'):
            self.sp = SignalProc.SignalProc(self.config['window_width'], self.config['incr'])
        start = "Tally,Night,Site,Detector,Detector Name,Bat species (L or S), Time of bat pass (24 hour clock e.g. 23:41:11),Length of bat pass (s),Feeding buzz present (yes/no)\n"
        output = start
        dt=0.002909090909090909
        if not os.path.isdir(dirName):
           print("Folder doesn't exist")
           return 0
        tally = 0
        for root, dirs, files in os.walk(dirName, topdown=True):
            nfiles = len(files)
            if nfiles > 0:
                for count in range(nfiles):
                    filename = files[count]
                    if filename.endswith('.data'):
                        segments = Segment.SegmentList()
                        segments.parseJSON(os.path.join(root, filename))
                        if len(segments)>0:
                            # Get the length of the clicks from the spectrogram
                            fn = filename[:-5]
                            #print(fn,os.path.join(root, fn))
                            self.sp.readBmp(os.path.join(root, fn), rotate=False,silent=True)
                            self.sampleRate = self.sp.sampleRate
                            res = self.ClickSearch(self.sp.sg,None,virginia=False)
                            if res is not None:
                                length = "{:.2f}".format((res[1]-res[0])*dt)
                            else:
                                length = str(0)
                            #print("Length "+length)
                            seg = segments[0]
                            #print(seg)
                            c = [lab["certainty"] for lab in seg[4]]
                            s = [lab["species"] for lab in seg[4]]
                            if len(c)>1:
                                label = 'L,S'
                            else:
                                if s[0] == 'Long-tailed bat':
                                    label = 'L'
                                elif s[0] == 'Short-tailed bat':
                                    label = 'S'
                        else:
                            length = "0"
                            label = ''
                        #print("label "+label)

                        # DOC format
                        # night comes from the directory
                        night = root[-2:]+"/"+root[-4:-2]+"/"+root[-6:-4]
                        folder = root.split("/")[-2]
                        detname = folder.split(" ")[-2]
                        #print(detname,folder)
                        #print("night "+night)
                        #night = filename[6:8]+"/"+filename[4:6]+"/"+filename[2:4]
                        # time comes from file
                        time = filename[9:11]+":"+filename[11:13]+":"+filename[13:15]
                        #print("time "+time)
                        
                        output+= str(tally)+","+night+",,,"+detname+","+label+","+time+","+length+",\n"
                        tally += 1
        # Now write the file if necessary
        if output != start:
            file = open(os.path.join(dirName, savefile), 'w')
            print("writing to", os.path.join(dirName, savefile))
            file.write(output)
            file.write("\n")
            file.close()
            output = start

    def exportToBatSearch(self,dirName,savefile='BatData.xml',threshold1=0.85,threshold2=0.7):
        # Write out a file that can be used for BatSearch import
        # For now, looks like the xml file used there
        # Assumes that dirName is a survey folder and the structure beneath is something like Rx/Bat/Date
        # No error checking
        operator = "AviaNZ 3.0"
        site = "Nowhere"
        # BatSeach codes
        namedict = {"Unassigned":0, "Non-bat":1, "Unknown":2, "Long Tail":3, "Short Tail":4, "Possible LT":5, "Possible ST":6, "Both":7}
        # File header
        start = "<?xml version=\"1.0\"?>\n<ArrayOfBatRecording xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\" xmlns:xsd=\"http://www.w3.org/2001/XMLSchema\">"
        output = start
        if not os.path.isdir(dirName):
            print("Folder doesn't exist")
            return 0
        for root, dirs, files in os.walk(dirName, topdown=True):
            nfiles = len(files)
            if nfiles > 0:
                for count in range(nfiles):
                    filename = files[count]
                    if filename.endswith('.data'):
                        segments = Segment.SegmentList()
                        segments.parseJSON(os.path.join(root, filename))
                        if len(segments)>0:
                            seg = segments[0]
                            print(seg)
                            c = [lab["certainty"] for lab in seg[4]]
                            s = [lab["species"] for lab in seg[4]]
                            if len(c)>1:
                                label = 'Both'
                            else:
                                if c[0]>=threshold1:
                                    if s[0] == 'Long-tailed bat':
                                        label = 'Long Tail'
                                    elif s[0] == 'Short-tailed bat':
                                        label = 'Short Tail'
                                elif threshold2 is not None:
                                    if c[0]>threshold2:
                                        if s[0] == 'Long-tailed bat':
                                            label = 'Possible LT'
                                        elif s[0] == 'Short-tailed bat':
                                            label = 'Possible ST'
                                elif threshold2 is None:
                                    if s[0] == 'Long-tailed bat':
                                        label = 'Possible LT'
                                    elif s[0] == 'Short-tailed bat':
                                        label = 'Possible ST'
                                else:
                                    label = 'Non-bat'
                        else:
                            # TODO: which?
                            label = 'Non-bat'
                            #label = 'Unassigned'
                        # This is the text for the file
                        s1 = "<BatRecording>\n"
                        s2 = "<AssignedBatCategory>"+str(namedict[label])+"</AssignedBatCategory>\n"
                        s3 = "<AssignedSite>"+site+"</AssignedSite>\n"
                        s4 = "<AssignedUser>"+operator+"</AssignedUser>\n"
                        # DOC format
                        s5 = "<RecTime>"+filename[:4]+"-"+filename[4:6]+"-"+filename[6:8]+"T"+filename[9:11]+":"+filename[11:13]+":"+filename[13:15]+"</RecTime>\n"
                        s6 = "<RecordingFileName>"+filename[:-5]+"</RecordingFileName>\n"
                        s7 = "<RecordingFolderName>.\\"+os.path.relpath(root, dirName)+"</RecordingFolderName>\n"
                        s8 = "<MeasureTimeFrom>0</MeasureTimeFrom>\n"
                        s9 = "</BatRecording>\n"
                        output+= s1+s2+s3+s4+s5+s6+s7+s8+s9
                # Now write the file if necessary
                if output != start:
                    output += "</ArrayOfBatRecording>\n"
                    file = open(os.path.join(root, savefile), 'w')
                    print("writing to", os.path.join(root, savefile))
                    file.write(output)
                    file.write("\n")
                    file.close()
                    output = start
    
        return 1

    def exportToBatSearch_NP(self, dirName, savefile='BatData.xml'):
        # Write out a file that can be used for BatSearch import
        # For now, looks like the xml file used there
        # Assumes that dirName is a survey folder and the structure beneath is something like Rx/Bat/Date
        # No error checking
        operator = "AviaNZ 3.1"
        site = "Nowhere"
        # BatSeach codes
        namedict = {"Unassigned": 0, "Non-bat": 1, "Unknown": 2, "Long Tail": 3, "Short Tail": 4, "Possible LT": 5,
                    "Possible ST": 6, "Both": 7}
        # File header
        start = "<?xml version=\"1.0\"?>\n<ArrayOfBatRecording xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\" xmlns:xsd=\"http://www.w3.org/2001/XMLSchema\">"
        output = start
        if not os.path.isdir(dirName):
            print("Folder doesn't exist")
            return 0
        for root, dirs, files in os.walk(dirName, topdown=True):
            nfiles = len(files)
            if nfiles > 0:
                for count in range(nfiles):
                    filename = files[count]
                    if filename.endswith('.data'):
                        segments = Segment.SegmentList()
                        segments.parseJSON(os.path.join(root, filename))
                        if len(segments) > 0:
                            seg = segments[0]
                            print(seg)
                            c = [lab["certainty"] for lab in seg[4]]
                            s = [lab["species"] for lab in seg[4]]
                            if c[0] == 100:
                                if s[0] == 'Long-tailed bat':
                                    label = 'Long Tail'
                                elif s[0] == 'Short-tailed bat':
                                    label = 'Short Tail'
                            else:
                                if s[0] == 'Long-tailed bat':
                                    label = 'Possible LT'
                                elif s[0] == 'Short-tailed bat':
                                    label = 'Possible ST'
                        else:
                            label = 'Non-bat'
                            # label = 'Unassigned'
                        # This is the text for the file
                        s1 = "<BatRecording>\n"
                        s2 = "<AssignedBatCategory>" + str(namedict[label]) + "</AssignedBatCategory>\n"
                        s3 = "<AssignedSite>" + site + "</AssignedSite>\n"
                        s4 = "<AssignedUser>" + operator + "</AssignedUser>\n"
                        # DOC format
                        s5 = "<RecTime>" + filename[:4] + "-" + filename[4:6] + "-" + filename[6:8] + "T" + filename[
                                                                                                            9:11] + ":" + filename[
                                                                                                                          11:13] + ":" + filename[
                                                                                                                                         13:15] + "</RecTime>\n"
                        s6 = "<RecordingFileName>" + filename[:-5] + "</RecordingFileName>\n"
                        s7 = "<RecordingFolderName>.\\" + os.path.basename(root) + "</RecordingFolderName>\n"
                        s8 = "<MeasureTimeFrom>0</MeasureTimeFrom>\n"
                        s9 = "</BatRecording>\n"
                        output += s1 + s2 + s3 + s4 + s5 + s6 + s7 + s8 + s9
                # Now write the file if necessary
                if output != start:
                    output += "</ArrayOfBatRecording>\n"
                    file = open(os.path.join(root, savefile), 'w')
                    print("writing to", os.path.join(root, savefile))
                    file.write(output)
                    file.write("\n")
                    file.close()
                    output = start

        return 1

    def exportBatSurvey(self,dirName,responses,threshold1=0.85):
        import datetime as dt
        # Export an excel file for the Bat survey database
        # TODO: turn into full excel?
        if responses is None:
            responses = ['',self.config['operator'],'','ABM','','','','','']

        dates = []
        for root, dirs, files in os.walk(dirName):
            # Read the dates
            for d in dirs:
                if d.isdigit():
                    dates.append(d)

        if len(dates)==0:
            print("ERROR: no suitable folders found")
            return 0
        else:
            print("Dates:", dates)

        dates = np.array(dates)
        dates = np.unique(dates)
        dates = np.sort(dates)

        # skip unparseable strings
        dates_formatted = []
        for d in dates:
            try:
                d_f = dt.datetime.strptime(d, '%Y%m%d').date()
                dates_formatted.append(d_f)
            except ValueError:
                print("Warning: directory %s does not look like a date" % d)

        if len(dates_formatted)==0:
            print("ERROR: none of the directory names were date-like")
            return 0

        # get first, last, and total number of nights present in the data
        start = dates_formatted[0]
        end = dates_formatted[-1]
        totalnights = len(dates_formatted)

        # LT then ST
        species = np.zeros(2,dtype=int)

        for root, dirs, files in os.walk(dirName,topdown=True):
            for filename in files:
                if filename.endswith('.data'):
                    segments = Segment.SegmentList()
                    segments.parseJSON(os.path.join(root, filename))
                    if len(segments)>0:
                        seg = segments[0]
                        c = [lab["certainty"] for lab in seg[4]]
                        s = [lab["species"] for lab in seg[4]]
                        if len(c)>1:
                            species[0] += 1
                            species[1] += 1
                        else:
                            # ignoring possibles, since there should be some definites if it is real.
                            if c[0]>threshold1:
                                if s[0] == 'Long-tailed bat':
                                    species[0] += 1
                                elif s[0] == 'Short-tailed bat':
                                    species[1] += 1

        f = open(os.path.join(dirName,'BatDB.csv'),'w')

        f.write('Data Source,Observer,Survey method,Species,Passes,Date,Detector type,Date recorder put out,Date recorder collected,No. of nights out,Effective nights out,Notes,Eastings,Northings,Site name,Region\n')

        # TODO: Get effective days (how?) I think it is temperature > 7 degrees
        line = responses[0]+','+responses[1]+','+responses[2]+','
        if species[0] > 0 and species[1] > 0:
            line = line + 'Both species detected'+','+str(species[0]+species[1])+','
        elif species[0] > 0:
            line = line + 'Chalinolobus tuberculatus'+','+str(species[0])+','
        elif species[1] > 0:
            line = line + 'Mystacina tuberculata'+','+str(species[1])+','
        else:
            line = line + 'No bat species detected'+','+'0'+','
        line = line + str(start)+','+responses[3]+','+str(start)+','+str(end)+','+str(totalnights)+','+str(totalnights)+','+responses[4]+','+responses[5]+','+responses[6]+','+responses[7]+','+responses[8]+'\n'
        f.write(line)
        f.close()

    def exportToBatSearchCSV(self,dirName,writefile="BatResults.csv",threshold1=0.85,threshold2=0.7):
        # This produces a csv file that looks like the one from Bat Search. 

        f = open(os.path.join(dirName,writefile),'w')
        f.write('Date,Time,AssignedSite,Category,Foldername,Filename,Observer\n')
        for root, dirs, files in os.walk(dirName):
            dirs.sort()
            files.sort()
            for filename in files:
                if filename.endswith('.data'):
                    segments = Segment.SegmentList()
                    segments.parseJSON(os.path.join(root, filename))
                    if len(segments)>0:
                        seg = segments[0]
                        c = [lab["certainty"] for lab in seg[4]]
                        s = [lab["species"] for lab in seg[4]]
                        if len(c)>1:
                            label = 'Both'
                        else:
                            if c[0]>threshold1:
                                if s[0] == 'Long-tailed bat':
                                    label = 'Long Tail'
                                elif s[0] == 'Short-tailed bat':
                                    label = 'Short Tail'
                            elif c[0]>threshold2:
                                if s[0] == 'Long-tailed bat':
                                    label = 'Possible LT'
                                elif s[0] == 'Short-tailed bat':
                                    label = 'Possible ST'
                            else:
                                label = '' #Non-bat'
                    else:
                        label = '' #'Non-bat'
                    # Assumes DOC format
                    d = filename[6:8]+'/'+filename[4:6]+'/'+filename[:4]+','
                    if d[0] == '0':
                        d = d[1:]
                    if int(filename[9:11]) < 13:
                        if filename[9:11] == '00':
                            t = str(int(filename[9:11])+12)+':'+filename[11:13]+':'+filename[13:15]+' a.m.,'
                        else:
                            t = filename[9:11]+':'+filename[11:13]+':'+filename[13:15]+' a.m.,'
                    else:
                        t = str(int(filename[9:11])-12)+':'+filename[11:13]+':'+filename[13:15]+' p.m.,'
                    if t[0] == '0':
                        t = t[1:]
                    # Assume that directory structure is recorder - date
                    if label == '':
                        rec = ',Unassigned'
                        op = ''
                    else:
                        rec = root.split('/')[-3]
                        op = 'Moira Pryde'
                    date = '.\\'+root.split('/')[-1]


class GentleExitException(Exception):
    """ To allow tracking user-requested aborts, instead of using C-style returns. """
    pass

