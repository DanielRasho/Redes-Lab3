from enum import Enum

class Types(Enum):
    MESSAGE = 1 # Send message
    ECHO = 2 # Medir distancias
    INFO = 3 # Enrutamiento
    HELLO = 4 # Initializatino

    def __init__(self, algorithm, type, topology, id, ttl = 5):
        self.algorithm = algorithm
        self.neightborns_table = {}
        self.id = id
        self.topology = topology

    def discover():
        '''
            Fills the neighbor table
        '''
        pass
    
    def listen():
        '''
        Callback function to receive and log other messages.
        SHOULD CREATE ON THREAD PER MESSAGE RECEIVED.
        '''
        pass
        
    def route():
        '''
            Based on the current neightbors table choses the next routing to send the message to
        '''
        
    def forward(msg, destination):
        '''
        Sends message to a destination router
        '''
        pass
    

'''Josue'''
def Dijkstra(topology = {}):
    pass

'''Josue'''
def Flooding(lifetime = 5):
    pass

'''Fabricio'''
def LinkStateRouting(topology = {}, lifetime = 5):
    pass