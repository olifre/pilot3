#!/usr/bin/env python
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Paul Nilsson, paul.nilsson@cern.ch, 2018

# This module contains implementations of job monitoring tasks

import os
import time
from subprocess import PIPE
from glob import glob

from pilot.common.errorcodes import ErrorCodes
from pilot.control.job import send_state
from pilot.util.auxiliary import get_logger
from pilot.util.config import config, human2bytes
from pilot.util.container import execute
from pilot.util.filehandling import get_directory_size, remove_files
from pilot.util.loopingjob import looping_job
from pilot.util.parameters import convert_to_int
from pilot.util.processes import get_instant_cpu_consumption_time, kill_processes
from pilot.util.workernode import get_local_disk_space

import logging
logger = logging.getLogger(__name__)

errors = ErrorCodes()


def job_monitor_tasks(job, mt, args):
    """
    Perform the tasks for the job monitoring, ending with sending the heartbeat.
    The function is called once a minute. Individual checks will be performed at any desired time interval (>= 1
    minute).

    :param job: job object.
    :param mt: `MonitoringTime` object.
    :param args: Pilot arguments (e.g. containing queue name, queuedata dictionary, etc).
    :return: exit code (int), diagnostics (string).
    """

    exit_code = 0
    diagnostics = ""

    log = get_logger(job.jobid)
    current_time = int(time.time())

    # update timing info for running jobs (to avoid an update after the job has finished)
    if job.state == 'running':
        cpuconsumptiontime = get_instant_cpu_consumption_time(job.pid)
        job.cpuconsumptiontime = int(cpuconsumptiontime)
        job.cpuconsumptionunit = "s"
        job.cpuconversionfactor = 1.0
        log.info('CPU consumption time for pid=%d: %f (rounded to %d)' %
                 (job.pid, cpuconsumptiontime, job.cpuconsumptiontime))

        # check memory usage (optional) for jobs in running state
        exit_code, diagnostics = verify_memory_usage(current_time, mt, job)
        if exit_code != 0:
            return exit_code, diagnostics

    # should the proxy be verified?
    if args.verify_proxy:
        exit_code, diagnostics = verify_user_proxy(current_time, mt)
        if exit_code != 0:
            return exit_code, diagnostics

    # is it time to check for looping jobs?
    exit_code, diagnostics = verify_looping_job(current_time, mt, job)
    if exit_code != 0:
        return exit_code, diagnostics

    # is the job using too much space?
    exit_code, diagnostics = verify_disk_usage(current_time, mt, job)
    if exit_code != 0:
        return exit_code, diagnostics

    # are the output files within allowed limits?

    # make sure that any utility commands are still running
    if job.utilities != {}:
        job = utility_monitor(job)

    # send heartbeat
    send_state(job, args, 'running')

    return exit_code, diagnostics


def verify_memory_usage(current_time, mt, job):
    """
    Verify the memory usage (optional).
    Note: this function relies on a stand-alone memory monitor tool that may be executed by the Pilot.

    :param current_time: current time at the start of the monitoring loop (int).
    :param mt: measured time object.
    :param job: job object.
    :return: exit code (int), error diagnostics (string).
    """

    pilot_user = os.environ.get('PILOT_USER', 'generic').lower()
    memory = __import__('pilot.user.%s.memory' % pilot_user, globals(), locals(), [pilot_user], -1)

    if not memory.allow_memory_usage_verifications():
        return 0, ""

    # is it time to verify the memory usage?
    memory_verification_time = convert_to_int(config.Pilot.memory_usage_verification_time, default=60)
    if current_time - mt.get('ct_memory') > memory_verification_time:
        # is the used memory within the allowed limit?
        exit_code, diagnostics = memory.memory_usage(job)
        if exit_code != 0:
            return exit_code, diagnostics
        else:
            # update the ct_proxy with the current time
            mt.update('ct_memory')

    return 0, ""


def verify_user_proxy(current_time, mt):
    """
    Verify the user proxy.
    This function is called by the job_monitor_tasks() function.

    :param current_time: current time at the start of the monitoring loop (int).
    :param mt: measured time object.
    :return: exit code (int), error diagnostics (string).
    """

    pilot_user = os.environ.get('PILOT_USER', 'generic').lower()
    userproxy = __import__('pilot.user.%s.proxy' % pilot_user, globals(), locals(), [pilot_user], -1)

    # is it time to verify the proxy?
    proxy_verification_time = convert_to_int(config.Pilot.proxy_verification_time, default=600)
    if current_time - mt.get('ct_proxy') > proxy_verification_time:
        # is the proxy still valid?
        exit_code, diagnostics = userproxy.verify_proxy()
        if exit_code != 0:
            return exit_code, diagnostics
        else:
            # update the ct_proxy with the current time
            mt.update('ct_proxy')

    return 0, ""


def verify_looping_job(current_time, mt, job):
    """
    Verify that the job is not looping.

    :param current_time: current time at the start of the monitoring loop (int).
    :param mt: measured time object.
    :param job: job object.
    :return: exit code (int), error diagnostics (string).
    """

    log = get_logger(job.jobid)

    looping_verifiction_time = convert_to_int(config.Pilot.looping_verifiction_time, default=600)
    if current_time - mt.get('ct_looping') > looping_verifiction_time:
        # is the job looping?
        try:
            exit_code, diagnostics = looping_job(job, mt)
        except Exception as e:
            exit_code = errors.UNKNOWNEXCEPTION
            diagnostics = 'exception caught in looping job algorithm: %s' % e
            log.warning(diagnostics)
            return exit_code, diagnostics
        else:
            if exit_code != 0:
                return exit_code, diagnostics

        # update the ct_proxy with the current time
        mt.update('ct_looping')

    return 0, ""


def verify_disk_usage(current_time, mt, job):
    """
    Verify the disk usage.
    The function checks 1) payload stdout size, 2) local space, 3) work directory size.

    :param current_time: current time at the start of the monitoring loop (int).
    :param mt: measured time object.
    :param job: job object.
    :return: exit code (int), error diagnostics (string).
    """

    disk_space_verification_time = convert_to_int(config.Pilot.disk_space_verification_time, default=300)
    if current_time - mt.get('ct_diskspace') > disk_space_verification_time:
        # time to check the disk space

        # check the size of the payload stdout
        exit_code, diagnostics = check_payload_stdout(job)
        if exit_code != 0:
            return exit_code, diagnostics

        # check the local space, if it's enough left to keep running the job
        exit_code, diagnostics = check_local_space()
        if exit_code != 0:
            return exit_code, diagnostics

        # check the size of the workdir
        exit_code, diagnostics = check_work_dir(job)
        if exit_code != 0:
            return exit_code, diagnostics

        # update the ct_diskspace with the current time
        mt.update('ct_diskspace')

    return 0, ""


def utility_monitor(job):
    """
    Make sure that any utility commands are still running.
    In case a utility tool has crashed, this function may restart the process.
    The function is used by the job monitor thread.

    :param job: job object.
    :return: updated job object.
    """

    pilot_user = os.environ.get('PILOT_USER', 'generic').lower()
    usercommon = __import__('pilot.user.%s.common' % pilot_user, globals(), locals(), [pilot_user], -1)

    log = get_logger(job.jobid)

    # loop over all utilities
    for utcmd in job.utilities.keys():

        # make sure the subprocess is still running
        utproc = job.utilities[utcmd][0]
        if not utproc.poll() is None:
            # if poll() returns anything but None it means that the subprocess has ended - which it
            # should not have done by itself
            utility_subprocess_launches = job.utilities[utcmd][1]
            if utility_subprocess_launches <= 5:
                log.warning('dectected crashed utility subprocess - will restart it')
                utility_command = job.utilities[utcmd][2]

                try:
                    proc1 = execute(utility_command, workdir=job.workdir, returnproc=True, usecontainer=True,
                                    stdout=PIPE, stderr=PIPE, cwd=job.workdir, queuedata=job.infosys.queuedata)
                except Exception as e:
                    log.error('could not execute: %s' % e)
                else:
                    # store process handle in job object, and keep track on how many times the
                    # command has been launched
                    job.utilities[utcmd] = [proc1, utility_subprocess_launches + 1, utility_command]
            else:
                log.warning('dectected crashed utility subprocess - too many restarts, will not restart %s again' %
                            utcmd)
        else:
            # log.info('utility %s is still running' % utcmd)

            # check the utility output (the selector option adds a substring to the output file name)
            filename = usercommon.get_utility_command_output_filename(utcmd, selector=True)
            path = os.path.join(job.workdir, filename)
            if os.path.exists(path):
                log.info('file: %s exists' % path)
            else:
                log.warning('file: %s does not exist' % path)
    return job


def get_local_size_limit_stdout(bytes=True):
    """
    Return a proper value for the local size limit for payload stdout (from config file).

    :param bytes: boolean (if True, convert kB to Bytes).
    :return: size limit (int).
    """

    try:
        localsizelimit_stdout = int(config.Pilot.local_size_limit_stdout)
    except Exception as e:
        localsizelimit_stdout = 2097152
        logger.warning('bad value in config for local_size_limit_stdout: %s (will use value: %d kB)' %
                       (e, localsizelimit_stdout))

    # convert from kB to B
    if bytes:
        localsizelimit_stdout *= 1024

    return localsizelimit_stdout


def check_payload_stdout(job):
    """
    Check the size of the payload stdout.

    :param job: job object.
    :return: exit code (int), diagnostics (string).
    """

    exit_code = 0
    diagnostics = ""

    log = get_logger(job.jobid)

    # get list of log files
    file_list = glob(os.path.join(job.workdir, 'log.*'))

    # is this a multi-trf job?
    n_jobs = job.jobparams.count("\n") + 1
    for _i in range(n_jobs):
        # get name of payload stdout file created by the pilot
        _stdout = config.Payload.payloadstdout
        if n_jobs > 1:
            _stdout = _stdout.replace(".txt", "_%d.txt" % (_i + 1))

        # add the primary stdout file to the fileList
        file_list.append(os.path.join(job.workdir, _stdout))

    # now loop over all files and check each individually (any large enough file will fail the job)
    for filename in file_list:

        if "job.log.tgz" in filename:
            log.info("skipping file size check of file (%s) since it is a special log file" % (filename))
            continue

        if os.path.exists(filename):
            try:
                # get file size in bytes
                fsize = os.path.getsize(filename)
            except Exception as e:
                log.warning("could not read file size of %s: %s" % (filename, e))
            else:
                # is the file too big?
                localsizelimit_stdout = get_local_size_limit_stdout()
                if fsize > localsizelimit_stdout:
                    diagnostics = "Payload stdout file too big: %d B (larger than limit %d B)" % \
                                  (fsize, localsizelimit_stdout)
                    log.warning(diagnostics)
                    # kill the job
                    kill_processes(job.pid)
                    job.state = "failed"
                    job.piloterrorcodes, job.piloterrordiags = errors.add_error_code(errors.STDOUTTOOBIG)

                    # remove the payload stdout file after the log extracts have been created

                    # remove any lingering input files from the work dir
                    if job.infiles:
                        # remove any lingering input files from the work dir
                        exit_code = remove_files(job.workdir, job.infiles)
                else:
                    log.info("payload stdout (%s) within allowed size limit (%d B): %d B" %
                             (_stdout, localsizelimit_stdout, fsize))
        else:
            log.info("skipping file size check of payload stdout file (%s) since it has not been created yet" % _stdout)

    return exit_code, diagnostics


def check_local_space():
    """
    Do we have enough local disk space left to run the job?

    :return: pilot error code (0 if success, NOLOCALSPACE if failure)
    """

    ec = 0
    diagnostics = ""

    # is there enough local space to run a job?
    spaceleft = int(get_local_disk_space(os.getcwd())) * 1024 ** 2  # B (diskspace is in MB)
    free_space_limit = human2bytes(config.Pilot.free_space_limit)
    if spaceleft <= free_space_limit:
        diagnostics = 'too little space left on local disk to run job: %d B (need > %d B)' %\
                      (spaceleft, free_space_limit)
        ec = errors.NOLOCALSPACE
        logger.warning(diagnostics)
    else:
        logger.info('sufficient remaining disk space (%d B)' % spaceleft)

    return ec, diagnostics


def check_work_dir(job):
    """
    Check the size of the work directory.
    The function also updates the workdirsizes list in the job object.

    :param job: job object.
    :return: exit code (int), error diagnostics (string)
    """

    exit_code = 0
    diagnostics = ""

    log = get_logger(job.jobid)

    if os.path.exists(job.workdir):
        # get the limit of the workdir
        maxwdirsize = get_max_allowed_work_dir_size(job.infosys.queuedata)

        if os.path.exists(job.workdir):
            workdirsize = get_directory_size(directory=job.workdir)

            # is user dir within allowed size limit?
            if workdirsize > maxwdirsize:

                diagnostics = "work directory (%s) is too large: %d B (must be < %d B)" % \
                              (job.workdir, workdirsize, maxwdirsize)
                log.fatal("%s" % diagnostics)

                cmd = 'ls -altrR %s' % job.workdir
                exit_code, stdout, stderr = execute(cmd, mute=True)
                log.info("%s: %s" % (cmd + '\n', stdout))

                # kill the job
                # pUtil.createLockFile(True, self.__env['jobDic'][k][1].workdir, lockfile="JOBWILLBEKILLED")
                kill_processes(job.pid)
                job.state = 'failed'
                job.piloterrorcodes, job.piloterrordiags = errors.add_error_code(errors.USERDIRTOOLARGE)

                # remove any lingering input files from the work dir
                if job.infiles != []:
                    exit_code = remove_files(job.workdir, job.infiles)

                    # remeasure the size of the workdir at this point since the value is stored below
                    workdirsize = get_directory_size(directory=job.workdir)
            else:
                log.info("size of work directory %s: %d B (within %d B limit)" %
                         (job.workdir, workdirsize, maxwdirsize))

            # Store the measured disk space (the max value will later be sent with the job metrics)
            if workdirsize > 0:
                job.add_workdir_size(workdirsize)
        else:
            log.warning('job work dir does not exist: %s' % job.workdir)
    else:
        log.warning('skipping size check of workdir since it has not been created yet')

    return exit_code, diagnostics


def get_max_allowed_work_dir_size(queuedata):
    """
    Return the maximum allowed size of the work directory.

    :param queuedata: job.infosys.queuedata object.
    :return: max allowed work dir size in Bytes (int).
    """

    try:
        maxwdirsize = int(queuedata.maxwdir) * 1024 ** 2  # from MB to B, e.g. 16336 MB -> 17,129,537,536 B
    except Exception:
        max_input_size = get_max_input_size()
        maxwdirsize = max_input_size + config.Pilot.local_size_limit_stdout * 1024
        logger.info("work directory size check will use %d B as a max limit (maxinputsize [%d B] + local size limit for"
                    " stdout [%d B])" % (maxwdirsize, max_input_size, config.Pilot.local_size_limit_stdout * 1024))
    else:
        logger.info("work directory size check will use %d B as a max limit" % maxwdirsize)

    return maxwdirsize


def get_max_input_size(queuedata, megabyte=False):
    """
    Return a proper maxinputsize value.

    :param queuedata: job.infosys.queuedata object.
    :param megabyte: return results in MB (Boolean).
    :return: max input size (int).
    """

    _maxinputsize = queuedata.maxwdir  # normally 14336+2000 MB
    max_input_file_sizes = 14 * 1024 * 1024 * 1024  # 14 GB, 14336 MB (pilot default)
    max_input_file_sizes_mb = 14 * 1024  # 14336 MB (pilot default)
    if _maxinputsize != "":
        try:
            if megabyte:  # convert to MB int
                _maxinputsize = int(_maxinputsize)  # MB
            else:  # convert to B int
                _maxinputsize = int(_maxinputsize) * 1024 * 1024  # MB -> B
        except Exception as e:
            logger.warning("schedconfig.maxinputsize: %s" % e)
            if megabyte:
                _maxinputsize = max_input_file_sizes_mb
            else:
                _maxinputsize = max_input_file_sizes
    else:
        if megabyte:
            _maxinputsize = max_input_file_sizes_mb
        else:
            _maxinputsize = max_input_file_sizes

    if megabyte:
        logger.info("max input size = %d MB (pilot default)" % _maxinputsize)
    else:
        logger.info("Max input size = %d B (pilot default)" % _maxinputsize)

    return _maxinputsize
