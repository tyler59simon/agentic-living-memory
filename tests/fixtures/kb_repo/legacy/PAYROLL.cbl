      * Monthly payroll run for the legacy HR system.
       IDENTIFICATION DIVISION.
       PROGRAM-ID. PAYROLL.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-HOURS        PIC 9(3).
       01  WS-RATE         PIC 9(3)V99.
       01  WS-OVERTIME     PIC 9(5)V99.
       PROCEDURE DIVISION.
       MAIN-PARA.
           PERFORM READ-HOURS.
           PERFORM COMPUTE-OVERTIME.
           STOP RUN.
       READ-HOURS.
           ACCEPT WS-HOURS.
       COMPUTE-OVERTIME.
      * Hours above 160 are paid at one and a half times the rate.
           IF WS-HOURS > 160
               COMPUTE WS-OVERTIME = (WS-HOURS - 160) * WS-RATE * 1.5
           END-IF.
