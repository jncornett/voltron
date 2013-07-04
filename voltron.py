#!/usr/bin/env python

from __future__ import print_function

import os
import sys
import socket
import asyncore
import threading
import argparse
import logging
import logging.config
import Queue
import termcolor
import struct

from collections import defaultdict

try:
    import cPickle as pickle
except:
    import pickle

try:
    import gdb
    in_gdb = True
except:
    in_gdb = False

try:
    import lldb
    in_lldb = True
except:
    in_lldb = False

try:
    import pygments
    import pygments.lexers
    import pygments.formatters
    have_pygments = True
except:
    have_pygments = False

SOCK = "/tmp/voltron.sock"
READ_MAX = 0xFFFF
LOG_CONFIG = {
        'version': 1,
        'formatters': {
            'standard': {'format': '[%(levelname)s]: %(message)s'}
        },
        'handlers': {
            'default': {
                'class': 'logging.StreamHandler',
                'formatter': 'standard'
            }
        },
        'loggers': {
            '': {
                'handlers': ['default'],
                'level': 'INFO',
                'propogate': True,
            }
        }
}

ADDR_FORMAT_64 = '0x{0:0=16X}'
ADDR_FORMAT_32 = '0x{0:0=8X}'
ADDR_FORMAT_16 = '0x{0:0=4X}'
SEGM_FORMAT_16 = '{0:0=4X}'

COLOURS = {
    'label': 'green',
    'value': 'grey',
    'modified': 'red',
    'header': 'blue',
    'flags': 'red',
}

DISASM_MAX = 32
STACK_MAX = 64

log = None
clients = []
queue = None
inst = None

def main(debugger=None, dict=None):
    global log, queue, inst

    # Configure logging
    logging.config.dictConfig(LOG_CONFIG)
    log = logging.getLogger('')

    # Set up queue
    queue = Queue.Queue()

    if in_gdb:
        # Load GDB command
        log.debug('Loading GDB command')
        print("Voltron loaded.")
        inst = VoltronGDBCommand()
    elif in_lldb:
        # Load LLDB command
        log.debug('Loading LLDB command')
        inst = VoltronLLDBCommand(debugger, dict)
    else:
        # Set up command line arg parser
        parser = argparse.ArgumentParser()
        parser.add_argument('--debug', '-d', action='store_true', help='print debug logging')
        subparsers = parser.add_subparsers(title='subcommands', description='valid subcommands', help='additional help')

        # Set up a subcommand for each view class 
        for cls in VoltronView.__subclasses__():
            cls.configure_subparser(subparsers)

        # And subcommands for the loathsome red-headed stepchildren
        StandaloneServer.configure_subparser(subparsers)
        GDB6Proxy.configure_subparser(subparsers)

        # Parse args
        args = parser.parse_args()
        if args.debug:
            log.setLevel(logging.DEBUG)

        # Instantiate and run the appropriate module
        inst = args.func(args)
        try:
            inst.run()
        except Exception as e:
            log.error("Exception running module {}: {}".format(inst.__class__.__name__, str(e)))
        except KeyboardInterrupt:
            pass
        inst.cleanup()
        log.info('Exiting')


#
# Server side code
#

class VoltronCommand (object):
    running = False

    def handle_command(self, command):
        global log
        if "start" in command:
            if 'debug' in command:
                log.setLevel(logging.DEBUG)
            self.start()
        elif "stop" in command:
            self.stop()
        elif "status" in command:
            self.status()
        elif "update" in command:
            self.update()
        else:
            print("Usage: voltron <start|stop|update|status>")

    def start(self):
        if not self.running:
            print("Starting voltron")
            self.running = True
            self.register_hooks()
            self.thread = ServerThread()
            self.thread.start()
        else:
            print("Already running")

    def stop(self):
        if self.running:
            print("Stopping voltron")
            self.unregister_hooks()
            self.thread.set_should_exit(True)
            self.thread.join(10)
            if self.thread.isAlive():
                print("Failed to stop voltron :<")
            self.running = False
        else:
            print("Not running")

    def status(self):
        if self.running:
            print("There are {} clients attached".format(len(clients)))
            for client in clients:
                print("{} registered with config: {}".format(client, str(client.registration['config'])))
        else:
            print("Not running")

    def update(self):
        log.debug("Updating clients")

        for client in filter(lambda c: c.registration['config']['update_on'] == 'stop', clients):
            event = {'msg_type': 'update', }

            if client.registration['config']['type'] == 'cmd':
                event['data'] = self.get_cmd_output(client.registration['config']['cmd'])
            elif client.registration['config']['type'] == 'register':
                event['data'] = self.get_registers()
            elif client.registration['config']['type'] == 'disasm':
                event['data'] = self.get_disasm()
            elif client.registration['config']['type'] == 'stack':
                event['data'] = {'data': self.get_stack(), 'sp': self.get_register('rsp')}
            elif client.registration['config']['type'] == 'bt':
                event['data'] = self.get_backtrace()
                
            queue.put((client, event))

    def register_hooks(self):
        pass

    def unregister_hooks(self):
        pass


# This is the actual GDB command. Should be able to just add an LLDB version I guess.
if in_gdb:
    class VoltronGDBCommand (VoltronCommand, gdb.Command):
        def __init__(self):
            super(VoltronCommand, self).__init__("voltron", gdb.COMMAND_NONE, gdb.COMPLETE_NONE)
            self.running = False

        def invoke(self, arg, from_tty):
            self.handle_command(arg)

        def register_hooks(self):
            gdb.events.stop.connect(self.stop_handler)

        def unregister_hooks(self):
            gdb.events.stop.disconnect(self.stop_handler)

        def stop_handler(self, event):
            self.update()

        def get_registers(self):
            log.debug('Getting registers')
            regs = ['rax','rbx','rcx','rdx','rbp','rsp','rdi','rsi','rip','r8','r9','r10','r11','r12','r13','r14','r15','cs','ds','es','fs','gs','ss']
            vals = {}
            for reg in regs:
                try:
                    vals[reg] = int(gdb.parse_and_eval('(long long)$'+reg)) & 0xFFFFFFFFFFFFFFFF
                except:
                    log.debug('Failed getting reg: ' + reg)
                    vals[reg] = 'N/A'
            vals['rflags'] = str(gdb.parse_and_eval('$eflags'))
            log.debug('Got registers: ' + str(vals))
            return vals

        def get_register(self, reg):
            log.debug('Getting register: ' + reg)
            return int(gdb.parse_and_eval('(long long)$'+reg)) & 0xFFFFFFFFFFFFFFFF

        def get_disasm(self):
            log.debug('Getting disasm')
            res = gdb.execute('x/{}i $rip'.format(DISASM_MAX), to_string=True)
            return res

        def get_stack(self):
            log.debug('Getting stack')
            rsp = int(gdb.parse_and_eval('(long long)$rsp')) & 0xFFFFFFFFFFFFFFFF
            res = str(gdb.selected_inferior().read_memory(rsp, STACK_MAX*16))
            return res

        def get_backtrace(self):
            log.debug('Getting backtrace')
            res = gdb.execute('bt', to_string=True)
            return res

        def get_cmd_output(self, cmd=None):
            if cmd:
                log.debug('Getting command output: ' + cmd)
                res = gdb.execute(cmd, to_string=True)
            else:
                res = "<No command>"
            return res


if in_lldb:
    # LLDB's initialisation routine
    def __lldb_init_module(debugger, dict):
        main(debugger, dict)

    def lldb_invoke(debugger, command, result, dict):
        inst.invoke(debugger, command, result, dict)

    class VoltronLLDBCommand (VoltronCommand):
        debugger = None

        def __init__(self, debugger, dict):
            self.debugger = debugger
            debugger.HandleCommand('command script add -f voltron.lldb_invoke voltron')
            self.running = False

        def invoke(self, debugger, command, result, dict):
            self.debugger = debugger
            self.handle_command(command)

        def register_hooks(self):
            self.debugger.HandleCommand('target stop-hook add -o \'voltron update\'')

        def unregister_hooks(self):
            # XXX: Fix this so it only removes our stop-hook
            self.debugger.HandleCommand('target stop-hook delete')

        def get_frame(self):
            return self.debugger.GetTargetAtIndex(0).process.selected_thread.GetFrameAtIndex(0)

        def get_registers(self):
            log.debug('Getting registers')
            frame = self.get_frame()
            regs = {x.name:int(x.value, 16) for x in list(list(frame.GetRegisters())[0])}
            return regs

        def get_register(self, reg):
            log.debug('Getting register: ' + reg)
            return self.get_registers()[reg]

        def get_disasm(self):
            log.debug('Getting disasm')
            res = self.get_cmd_output('disassemble -c {}'.format(DISASM_MAX))
            return res

        def get_stack(self):
            log.debug('Getting stack')
            rsp = self.get_register('rsp')
            error = lldb.SBError()
            res = lldb.debugger.GetTargetAtIndex(0).process.ReadMemory(rsp, STACK_MAX*16, error)
            return res

        def get_backtrace(self):
            log.debug('Getting backtrace')
            res = self.get_cmd_output('bt')
            return res

        def get_cmd_output(self, cmd=None):
            if cmd:
                log.debug('Getting command output: ' + cmd)
                res = lldb.SBCommandReturnObject()
                self.debugger.GetCommandInterpreter().HandleCommand(cmd, res)
                res = res.GetOutput()
            else:
                res = "<No command>"
            return res


class StandaloneServer (object):
    @classmethod
    def configure_subparser(cls, subparsers):
        sp = subparsers.add_parser('server', help='standalone server for debuggers without python support')
        sp.set_defaults(func=StandaloneServer)

    def __init__(self, args={}):
        self.args = args

    def run(self):
        log.debug("Starting standalone server")
        self.thread = ServerThread()
        self.thread.start()
        while True: pass

    def cleanup(self):
        log.info("Exiting")
        self.thread.set_should_exit(True)
        self.thread.join(10)


# Socket for talking to an individual client
class ClientHandler (asyncore.dispatcher):
    def __init__(self, sock):
        asyncore.dispatcher.__init__(self, sock)
        self.registration = None

    def handle_read(self):
        data = self.recv(READ_MAX)
        if data.strip() != "":
            try:
                msg = pickle.loads(data)
                log.debug('Received msg: ' + str(msg))
            except:
                log.error('Invalid message data: ' + data)

            if msg['msg_type'] == 'register':
                self.handle_register(msg)
            elif msg['msg_type'] == 'push_update':
                self.handle_push_update(msg)
            else:
                log.error('Invalid message type: ' + msg['msg_type'])

    def handle_register(self, msg):
        log.debug('Registering client {} with config: {}'.format(self, str(msg['config'])))
        self.registration = msg

    def handle_push_update(self, msg):
        log.debug('Got a push update from client {} of type {} with data: {}'.format(self, msg['update_type'], str(msg['data'])))
        event = {'msg_type': 'update', 'data': msg['data']}
        for client in clients:
            if client.registration != None and client.registration['config']['type'] == msg['update_type']:
                queue.put((client, event))
        self.send(pickle.dumps({'msg_type': 'ack'}))

    def handle_close(self):
        self.close()
        clients.remove(self)

    def send_event(self, event):
        log.debug('Sending event to client {}: {}'.format(self, event))
        self.send(pickle.dumps(event))


# Main server socket for accept()s
class Server (asyncore.dispatcher):
    def __init__(self, sockfile):
        asyncore.dispatcher.__init__(self)
        try:
            os.remove(SOCK)
        except:
            pass
        self.create_socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.bind(sockfile)
        self.listen(1)

    def handle_accept(self):
        global clients
        pair = self.accept()
        if pair is not None:
            sock, addr = pair
            try:
                client = ClientHandler(sock)
                clients.append(client)
            except Exception as e:
                log.error("Exception handling accept: " + str(e))


# Thread spun off when the plugin is started to listen for incoming client connections, and send out any
# events that have been queued by the hooks in the debugger command class
class ServerThread (threading.Thread):
    def run(self):
        global clients, queue

        # Create a server instance
        serv = Server(SOCK)
        self.lock = threading.Lock()
        self.set_should_exit(False)

        # Main event loop
        while not self.should_exit():
            # Check sockets for activity
            asyncore.loop(count=1, timeout=0.1)

            # Process any events in the queue
            while not queue.empty():
                client, event = queue.get()
                client.send_event(event)

        # Clean up
        serv.close()

    def should_exit(self):
        self.lock.acquire()
        r = self._should_exit
        self.lock.release()
        return r

    def set_should_exit(self, should_exit):
        self.lock.acquire()
        self._should_exit = should_exit
        self.lock.release()


#
# Client-side code
#

# Socket to register with the server and receive messages, calls view's render() method when a message comes in
class Client (asyncore.dispatcher):
    def __init__(self, view=None, config={}):
        asyncore.dispatcher.__init__(self)
        self.view = view
        self.config = config
        self.reg_info = None
        self.create_socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.connect(SOCK)

    def register(self):
        log.debug('Client {} registering with config: {}'.format(self, str(self.config)))
        msg = {'msg_type': 'register', 'config': self.config}
        log.debug('Sending: ' + str(msg))
        self.send(pickle.dumps(msg))

    def handle_read(self):
        data = self.recv(READ_MAX)
        try:
            msg = pickle.loads(data)
            log.debug('Received message: ' + str(msg))
            if self.view:
                self.view.render(msg)
        except:
            log.debug('Invalid message: ' + data)


# Parent class for all views
class VoltronView (object):
    DEFAULT_CONFIG = {
        'type': 'base',
        'clear': True,
        'show_header': True,
        'show_footer': True,
        'update_on': 'stop'
    }

    def __init__(self, args={}, config={}):
        log.debug('Loading view: ' + self.__class__.__name__)
        self.client = None
        self.config = config
        self.args = args
        os.system('tput civis')
        self.setup()
        self.connect()

    def setup(self):
        log.debug('Base view class setup')

    def cleanup(self):
        log.debug('Cleaning up view')
        os.system('tput cnorm')

    def connect(self):
        try:
            self.config = dict(self.DEFAULT_CONFIG.items() + self.config.items())
            self.client = Client(view=self, config=self.config)
            self.client.register()
        except Exception as e:
            log.error('Exception connecting: ' + str(e))
            raise e

    def run(self):
        os.system('clear')
        log.info('Waiting for an update from the debugger')
        asyncore.loop()

    def render(self, msg=None):
        log.warning('Might wanna implement render() in this view eh')

    def clear(self):
        # Clear the window - this sucks, should probably do it with ncurses at some stage
        os.system('clear')

    def window_size(self):
        # Get terminal size - this also sucks, but curses sucks more
        height, width = os.popen('stty size').read().split()
        height = int(height)
        width = int(width)
        return (height, width)

    def hexdump(self, src, length=16, sep='.', offset=0):
        FILTER = ''.join([(len(repr(chr(x))) == 3) and chr(x) or sep for x in range(256)])
        lines = []
        for c in xrange(0, len(src), length):
            chars = src[c:c+length]
            hex = ' '.join(["%02X" % ord(x) for x in chars])
            if len(hex) > 24:
                hex = "%s %s" % (hex[:24], hex[24:])
            printable = ''.join(["%s" % ((ord(x) <= 127 and FILTER[ord(x)]) or sep) for x in chars])
            lines.append("%s:  %-*s  |%s|\n" % (ADDR_FORMAT_64.format(offset+c), length*3, hex, printable))
        return ''.join(lines).strip()

    def format_header(self, title=None, left=None, right=None):
        if not self.config['show_header']: return ''

        height, width = self.window_size()

        # Left data
        header = ''
        if left != None:
            header = left

        # Dashes
        dashlen = width - len(header) - len(title)
        if dashlen < 0:
            dashlen = 1
        header += '-' * dashlen

        # Title
        header = termcolor.colored(header, COLOURS['header']) + termcolor.colored(title, 'white', attrs=['bold'])
        
        return header

    def format_footer(self, title=None, left=None, right=None):
        if not self.config['show_footer']: return ''

        height, width = self.window_size()
        dashlen = width
        footer = '-' * dashlen
        footer = termcolor.colored(footer, COLOURS['header']) 
        return footer


# Class to actually render the view
class RegisterView (VoltronView):
    FORMAT_INFO = {
        'x64': [
            {
                'regs':             ['rax','rbx','rcx','rdx','rbp','rsp','rdi','rsi','rip','r8','r9','r10','r11','r12','r13','r14','r15'],
                'label_format':     '{0:3s}:',
                'label_mod':        str.upper,
                'label_colour':     COLOURS['label'],
                'value_format':     ADDR_FORMAT_64,
                'value_mod':        None,
                'value_colour':     COLOURS['value'],
                'value_colour_mod': COLOURS['modified']
            },
            {
                'regs':             ['cs','ds','es','fs','gs','ss'],
                'label_format':     '{0}:',
                'label_mod':        str.upper,
                'label_colour':     COLOURS['label'],
                'value_format':     SEGM_FORMAT_16,
                'value_mod':        None,
                'value_colour':     COLOURS['value'],
                'value_colour_mod': COLOURS['modified']
            },
            {
                'regs':             ['rflags'],
                'value_format':     '{0}',
                'value_mod':        None
            },
        ]
    }
    FORMAT_DEFAULTS = {
        'label_format':     '{}:',
        'label_mod':        str.upper,
        'label_colour':     COLOURS['label'],
        'value_format':     ADDR_FORMAT_64,
        'value_mod':        None,
        'value_colour':     COLOURS['value'],
        'value_colour_mod': COLOURS['modified']
    }
    TEMPLATE_H = (
        "{raxl} {rax}  {rbxl} {rbx}  {rbpl} {rbp}  {rspl} {rsp}  {eflags}\n"
        "{rdil} {rdi}  {rsil} {rsi}  {rdxl} {rdx}  {rcxl} {rcx}  {ripl} {rip}\n"
        "{r8l} {r8}  {r9l} {r9}  {r10l} {r10}  {r11l} {r11}  {r12l} {r12}\n"
        "{r13l} {r13}  {r14l} {r14}  {r15l} {r15}\n"
        "{csl} {cs}  {dsl} {ds}  {esl} {es}  {fsl} {fs}  {gsl} {gs}  {ssl} {ss}\n"
    )
    TEMPLATE_V = (
        "{ripl} {rip}\n\n"
        "{raxl} {rax}\n{rbxl} {rbx}\n{rbpl} {rbp}\n{rspl} {rsp}\n"
        "{rdil} {rdi}\n{rsil} {rsi}\n{rdxl} {rdx}\n{rcxl} {rcx}\n"
        "{r8l} {r8}\n{r9l} {r9}\n{r10l} {r10}\n{r11l} {r11}\n{r12l} {r12}\n"
        "{r13l} {r13}\n{r14l} {r14}\n{r15l} {r15}\n"
        "{csl}  {cs}  {dsl}  {ds}\n{esl}  {es}  {fsl}  {fs}\n{gsl}  {gs}  {ssl}  {ss}\n"
        "    {rflags}\n"
    )

    last_regs = None

    @classmethod
    def configure_subparser(cls, subparsers):
        sp = subparsers.add_parser('reg', help='register view')
        sp.set_defaults(func=RegisterView)
        g = sp.add_mutually_exclusive_group()
        g.add_argument('--horizontal', '-o', action='store_true', help='horizontal orientation (default)', default=False)
        g.add_argument('--vertical', '-v', action='store_true', help='vertical orientation', default=True)

    def setup(self):
        self.config['type'] = 'register'

    def render(self, msg=None):
        self.clear()

        # Grab the appropriate template
        template = self.TEMPLATE_V if self.args.vertical else self.TEMPLATE_H

        # Process formatting settings
        data = defaultdict(lambda: '<n/a>')
        data.update(msg['data'])
        formats = self.FORMAT_INFO['x64']
        formatted = {}
        for fmt in formats:
            # Apply defaults where they're missing
            fmt = dict(self.FORMAT_DEFAULTS.items() + fmt.items())

            # Format the data for each register
            for reg in fmt['regs']:
                # Format the label
                label = fmt['label_format'].format(reg)
                if fmt['label_mod'] != None:
                    label = fmt['label_mod'](label)
                formatted[reg+'l'] =  termcolor.colored(label, fmt['label_colour'])

                # Format the value
                val = data[reg]
                if type(val) == str:
                    formatted[reg] = termcolor.colored(val, fmt['value_colour'])
                else:
                    colour = fmt['value_colour']
                    if self.last_regs == None or self.last_regs != None and val != self.last_regs[reg]:
                        colour = fmt['value_colour_mod']
                    val = fmt['value_format'].format(val)
                    if fmt['value_mod'] != None:
                        val = fmt['value_mod'](val)
                    formatted[reg] = termcolor.colored(val, colour)

        log.debug('Formatted: ' + str(formatted))
        print(self.format_header('[regs]'))
        print(template.format(**formatted), end='')
        print(self.format_footer(), end='')
        sys.stdout.flush()

        # Store the regs
        self.last_regs = data


class DisasmView (VoltronView):
    DISASM_SHOW_LINES = 16
    DISASM_SEP_WIDTH = 90

    @classmethod
    def configure_subparser(cls, subparsers):
        sp = subparsers.add_parser('disasm', help='disassembly view')
        sp.set_defaults(func=DisasmView)

    def setup(self):
        self.config['type'] = 'disasm'

    def render(self, msg=None):
        self.clear()
        height, width = self.window_size()

        # Get the disasm
        disasm = msg['data']
        disasm = '\n'.join(disasm.split('\n')[:height-2])

        # Pygmentize output
        if have_pygments:
            try:
                lexer = pygments.lexers.get_lexer_by_name('gdb')
                disasm = pygments.highlight(disasm, lexer, pygments.formatters.Terminal256Formatter())
            except Exception as e:
                log.warning('Failed to highlight disasm: ' + str(e))

        # Print output
        print(self.format_header('[code]'))
        print(disasm.rstrip())
        print(self.format_footer(), end='')
        sys.stdout.flush()


class StackView (VoltronView):
    STACK_SHOW_LINES = 16
    STACK_SEP_WIDTH = 90

    @classmethod
    def configure_subparser(cls, subparsers):
        sp = subparsers.add_parser('stack', help='stack view')
        sp.set_defaults(func=StackView)

    def setup(self):
        self.config['type'] = 'stack'

    def render(self, msg=None):
        self.clear()
        height, width = self.window_size()

        # Get the stack data
        data = msg['data']
        stack_raw = data['data']
        sp = data['sp']
        stack_raw = stack_raw[:(height-2)*16]

        # Hexdump it
        lines = self.hexdump(stack_raw, offset=sp).split('\n')
        lines.reverse()
        stack = '\n'.join(lines)

        # Print output
        sp_addr = '[0x{0:0=4x}:'.format(len(stack_raw)) + ADDR_FORMAT_64.format(sp) + ']'
        print(self.format_header('[stack]', left=sp_addr))
        print(stack.strip())
        print(self.format_footer(), end='')
        sys.stdout.flush()


class BacktraceView (VoltronView):
    @classmethod
    def configure_subparser(cls, subparsers):
        sp = subparsers.add_parser('bt', help='backtrace view')
        sp.set_defaults(func=BacktraceView)

    def setup(self):
        self.config['type'] = 'bt'

    def render(self, msg=None):
        self.clear()
        height, width = self.window_size()

        # Get the back trace data
        data = msg['data']
        lines = data.split('\n')
        pad = height - len(lines) - 2
        if pad < 0:
            pad = 0

        # Print output
        print(self.format_header('[backtrace]'))
        print(data.strip())
        if pad > 0:
            print('\n' * pad) 
        print(self.format_footer(), end='')
        sys.stdout.flush()


class CommandView (VoltronView):
    @classmethod
    def configure_subparser(cls, subparsers):
        sp = subparsers.add_parser('cmd', help='command view - specify a command to be run each time the debugger stops')
        sp.add_argument('command', action='store', help='command to run')
        sp.add_argument('--header', '-e', action='store_true', help='print header', default=False)
        sp.add_argument('--footer', '-f', action='store_true', help='print footer', default=False)
        sp.set_defaults(func=CommandView)

    def setup(self):
        self.config['type'] = 'cmd'
        self.config['cmd'] = self.args.command

    def render(self, msg=None):
        self.clear()
        print(self.format_header('[cmd:' + self.config['cmd'] + ']'))
        print(msg['data'].strip())
        print(self.format_footer(), end='')
        sys.stdout.flush()


# This class is called from the command line by GDBv6's stop-hook. The dumped registers and stack are collected,
# parsed and sent to the voltron standalone server, which then sends the updates out to any registered clients.
# I hate that this exists. Fuck GDBv6.
class GDB6Proxy (asyncore.dispatcher):
    REGISTERS = ['rax','rbx','rcx','rdx','rbp','rsp','rdi','rsi','rip','r8','r9','r10','r11','r12','r13','r14','r15','eflags']

    @classmethod
    def configure_subparser(cls, subparsers):
        sp = subparsers.add_parser('gdb6proxy', help='import a dump from GDBv6 and send it to the server')
        sp.add_argument('type', action='store', help='the type to proxy - reg or stack')
        sp.set_defaults(func=GDB6Proxy)

    def __init__(self, args={}):
        asyncore.dispatcher.__init__(self)
        self.args = args
        self.create_socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.connect(SOCK)

    def run(self):
        asyncore.loop()

    def handle_connect(self):
        if self.args.type == "reg":
            event = self.read_registers()
        elif self.args.type == "stack":
            event = self.read_stack()
        else:
            log.error("Invalid proxy type")
        log.debug("Pushing update to server")
        log.debug(str(event))
        self.send(pickle.dumps(event))

    def handle_read(self):
        data = self.recv(READ_MAX)
        msg = pickle.loads(data)
        if msg['msg_type'] != 'ack':
            log.error("Did not get ack: " + str(msg))
        self.close()

    def read_registers(self):
        log.debug("Parsing register data")
        data = {}
        for reg in GDB6Proxy.REGISTERS:
            try:
                with open('/tmp/voltron.reg.'+reg, 'r+b') as f:
                    if reg == 'eflags':
                        (val,) = struct.unpack('<L', f.read())
                    else:
                        (val,) = struct.unpack('<Q', f.read())
                data[reg] = val
            except Exception as e:
                log.warning("Exception reading register {}: {}".format(reg, str(e)))
                data[reg] = '<fail>'
        event = {'msg_type': 'push_update', 'update_type': 'register', 'data': data}
        return event

    def read_stack(self):
        log.debug("Parsing stack data")
        with open('/tmp/voltron.stack', 'r+b') as f:
            data = f.read()
        with open('/tmp/voltron.reg.rsp', 'r+b') as f:
            (rsp,) = struct.unpack('<Q', f.read())
        event = {'msg_type': 'push_update', 'update_type': 'stack', 'data': {'sp': rsp, 'data': data}}
        return event

    def cleanup(self):
        pass


if __name__ == "__main__":
    main()