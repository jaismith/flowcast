! Minimal reference driver for NOAA-OWP EXSNOW19 used to generate test fixtures.
! stdin: line 1 = idt_hours, line 2 = 12 params + elev + pa, line 3 = 11 ADC values,
!        then rows "year mo dy pcp_mm tmp_c" until EOF.
! stdout: "twe_mm raim_mm snowh_m snowfall_mm" per row.
program refdriver
  implicit none
  integer :: idt, iyr, imn, ida, ios, i
  real :: alat, scf, mfmax, mfmin, uadj, si, nmf, tipm, mbase, pxtemp, plwhc, daygm, elev, pa
  real :: adc(11), cs(19), pcp, tmp, raim, sneqv, snow, snowh, tprev
  read(*,*) idt
  read(*,*) alat, scf, mfmax, mfmin, uadj, si, nmf, tipm, mbase, pxtemp, plwhc, daygm, elev, pa
  read(*,*) (adc(i), i=1,11)
  cs = 0.0
  tprev = 0.0
  do
    read(*,*,iostat=ios) iyr, imn, ida, pcp, tmp
    if (ios /= 0) exit
    call exsnow19(idt*3600, idt, ida, imn, iyr, pcp, tmp, raim, sneqv, snow, snowh, &
         alat, scf, mfmax, mfmin, uadj, si, nmf, tipm, mbase, pxtemp, plwhc, daygm, elev, pa, adc, &
         cs, tprev)
    tprev = tmp
    write(*,'(4(ES16.8,1X))') sneqv*1000.0, raim, snowh, snow
  end do
end program refdriver
